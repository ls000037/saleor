import logging
from django.core.exceptions import ValidationError
from django.db import transaction
import graphene

from ....order.models import OrderEvent
from ....order import OrderStatus, OrderEvents, OrderChargeStatus
from ...core import ResolveInfo
from ...core.mutations import BaseMutation

from ...order.types import Order as OrderType
from saleor.order.models import Order as OrderModel, OrderLine as OrderLineModel

from saleor.graphql.core.types.common import OrderError
from ....order.error_codes import OrderErrorCode

from ...app.dataloaders import get_app_promise

from django.utils import timezone
from saleor.product.utils import product_type as product_type_utils
from saleor.order import OrderType as OrderTypeEnum
from saleor.order import utils as order_utils
from decimal import Decimal
from saleor.core.postgres import FlatConcatSearchVector
from saleor.order.search import prepare_order_search_vector_value
import time
import random
import os
import json
import requests
from base64 import b64encode, encodebytes
from Cryptodome.Hash import SHA256
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pkcs1_15

logger = logging.getLogger(__name__)

headers = {"Content-Type": "application/json",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36 Edg/96.0.1054.53"}


class OrderPayment(BaseMutation):
    # order = graphene.Field(OrderType, description="Placed order.")
    orders = graphene.List(OrderType, description="List of placed orders.")
    data = graphene.JSONString()

    class Arguments:
        order = graphene.ID(required=True, description="ID of an order.")
        product_promotion_id = graphene.ID(required=False,
                                           description="ID of the promotion product variant.")

    class Meta:
        description = "Create a new order from an existing checkout."
        error_type_class = OrderError

    # @classmethod
    # def get_gift_card(cls, info: ResolveInfo):
    #     user = info.context.user
    #     gift_card = GiftCard.objects.filter(used_by_id=user.id).first()
    #     if not gift_card:
    #         raise ValidationError(
    #             {
    #                 "gift_card": ValidationError(
    #                     "Points account not found or not active",
    #                     code=GiftCardErrorCode.NOT_FOUND.value,
    #                 )
    #             }
    #         )
    #     if not gift_card.is_active:
    #         raise ValidationError(
    #             {
    #                 "gift_card": ValidationError(
    #                     "Points account not found or not active",
    #                     code=GiftCardErrorCode.NOT_FOUND.value,
    #                 )
    #             }
    #         )
    #     return gift_card

    # 计算每个供应商的订单金额
    @classmethod
    def get_vendor_order_amount(cls, order_lines):
        vendor_order_amount = []
        for supplier, supplier_lines in order_lines.items():
            amount: Decimal = 0
            undiscounted_amount: Decimal = 0
            # platform_discount_amount: Decimal = 0
            # store_discount_amount: Decimal = 0

            for line in supplier_lines:
                amount += line.unit_price_gross_amount * line.quantity
                undiscounted_amount += line.undiscounted_total_price_net_amount
                # store_discount_amount += line.store_discount_amount
                # platform_discount_amount += line.platform_discount_amount
            vendor_order_amount.append({
                "supplier_id": supplier,
                "amount": amount,
                "undiscounted_amount": undiscounted_amount,
                # "store_discount_amount": store_discount_amount,
                # "platform_discount_amount": platform_discount_amount,
            })
        return vendor_order_amount

    @staticmethod
    def gener_auth(method, url, body, mchid, serial_no):
        timestamp = int(time.time())
        random_str = "".join(random.sample('QWERTYUIOPASDFGHJKLZXCVBNM123456789', 32))

        sign_list = [
            method,
            url,
            str(timestamp),
            random_str,
            body
        ]
        sign_str = '\n'.join(sign_list) + '\n'
        basepath = os.path.dirname(__file__)
        file_path = os.path.join(basepath, 'apiclient_key.pem')

        signer = pkcs1_15.new(RSA.importKey(open(file_path).read()))
        signature = signer.sign(SHA256.new(sign_str.encode("utf-8")))
        sign = encodebytes(signature).decode("utf-8").replace("\n", "")

        authorization = 'WECHATPAY2-SHA256-RSA2048' \
                        'mchid="{0}",' \
                        'nonce_str="{1}",' \
                        'signature="{2}",' \
                        'timestamp="{3}",' \
                        'serial_no="{4}"'. \
            format(mchid,
                   random_str,
                   sign,
                   timestamp,
                   serial_no
                   )
        return authorization

    @staticmethod
    def gener_paysign(appid, prepayid):
        noncestr = "".join(random.sample('QWERTYUIOPASDFGHJKLZXCVBNM123456789', 32))
        timestamp = int(time.time())
        sign_list = [
            appid,
            str(timestamp),
            noncestr,
            'prepay_id=' + prepayid
        ]
        sign_str = '\n'.join(sign_list) + '\n'
        basepath = os.path.dirname(__file__)
        file_path = os.path.join(basepath, 'apiclient_key.pem')

        signer = pkcs1_15.new(RSA.importKey(open(file_path).read()))
        signature = signer.sign(SHA256.new(sign_str.encode("utf-8")))
        sign = encodebytes(signature).decode("utf-8").replace("\n", "")
        return sign, noncestr, timestamp

    @classmethod
    def perform_mutation(cls, root, info: ResolveInfo, order, product_promotion_id=None,
                         metadata=None,
                         private_metadata=None):
        user = info.context.user
        if user is None:
            raise ValidationError(
                {
                    "user": ValidationError(
                        "You need to be authenticated to perform this action.",
                        code=OrderErrorCode.INVALID.value,
                    )
                }
            )

        product_promotion = None
        try:
            # FIXME
            order = cls.get_node_or_error(
                info,
                order,
                field="id",
                only_type=OrderType,
                code=OrderErrorCode.NOT_FOUND.value,
            )

            # 并且订单处于未支付状态
            if not order or order.user_id != user.id or order.status != OrderStatus.UNPAID:
                raise ValidationError(
                    {
                        "order": ValidationError(
                            "Order not found",
                            code=OrderErrorCode.NOT_FOUND.value
                        )
                    }
                )
            # gift_card = cls.get_gift_card(info)
            # 花费金额（包含运费）
            total_amount = order.total_gross_amount + order.shipping_price_gross_amount
            app = get_app_promise(info.context).get()

            orders = []
            # 绑定用户到秒杀商品

            with transaction.atomic():
                events = []
                supplier = None
                order_lines = order.lines.all()
                new_order_lines = {}
                product_type = None
                # 拆单,如果只有一个供应商，不需要拆单
                for line in order_lines:
                    product_type = product_type_utils.get_product_type(
                        line.variant.product.product_type)
                    if product_type is None:
                        raise ValidationError(
                            {
                                "product": ValidationError(
                                    # 未知的商品类型
                                    "Unknown product type",
                                    code=OrderErrorCode.INVALID.value
                                )
                            }
                        )

                    supplier = line.variant.product.supplier
                    if product_type == OrderTypeEnum.LOGISTICS:
                        if not supplier:
                            raise ValidationError(
                                {
                                    "product": ValidationError(
                                        "Supplier not found",
                                        code=OrderErrorCode.SUPPLIER_NOT_FOUND.value
                                    )
                                }
                            )
                        if supplier.id not in new_order_lines:
                            new_order_lines[supplier.id] = []
                        new_order_lines[supplier.id].append(line)

                # 拆单，有多个供应商的物流类商品
                if product_type == OrderTypeEnum.LOGISTICS and len(new_order_lines) > 1:
                    vendor_order_amount = cls.get_vendor_order_amount(new_order_lines)
                    for supplier, supplier_lines in new_order_lines.items():
                        total_amount: Decimal = 0
                        undiscounted_total_amount: Decimal = 0
                        # discount_store: Decimal = 0
                        # discount_plant: Decimal = 0
                        for order_amount in vendor_order_amount:
                            if order_amount["supplier_id"] == supplier:
                                total_amount = order_amount["amount"]
                                undiscounted_total_amount = order_amount[
                                    "undiscounted_amount"]
                                # discount_store = order_amount["store_discount_amount"]
                                # discount_plant = order_amount[
                                #     "platform_discount_amount"]
                                break

                        new_order = OrderModel.objects.create(
                            user=user,
                            billing_address=order.billing_address,
                            shipping_address=order.shipping_address,
                            language_code=order.language_code,
                            tracking_client_id=order.tracking_client_id,
                            channel=order.channel,
                            status=OrderStatus.PAID,
                            # shipping_method_name=order.shipping_method_name,
                            # shipping_price=order.shipping_price,
                            # shipping_price_net=order.shipping_price_net,
                            # shipping_price_gross=order.shipping_price_gross,
                            metadata=order.metadata,
                            private_metadata=order.private_metadata,
                            checkout_token=order.checkout_token,
                            payment_at=timezone.now(),
                            type=OrderTypeEnum.LOGISTICS,
                            supplier_id=supplier,
                            created_at=order.created_at,
                            user_email=order.user_email,
                            currency=order.currency,
                            total_net_amount=total_amount,
                            total_gross_amount=total_amount,
                            undiscounted_total_net_amount=undiscounted_total_amount,
                            undiscounted_total_gross_amount=undiscounted_total_amount,
                            # discount_plant=discount_plant,
                            # discount_store=discount_store,

                            # 已付款金额
                            total_charged_amount=total_amount,
                            charge_status=OrderChargeStatus.FULL,
                            origin=order.origin,

                            voucher=order.voucher,
                            # gift_cards=order.gift_cards,
                            customer_note=order.customer_note,
                            weight=order.weight,
                            should_refresh_prices=order.should_refresh_prices,
                        )

                        events.append(
                            OrderEvent(order=new_order,
                                       type=OrderEvents.ORDER_FULLY_PAID, user=user,
                                       app=app))

                        products_name = ""
                        for line in supplier_lines:
                            line.order = new_order
                            products_name += line.product_name + ","
                            line.save()
                        products_name = products_name[:-1]

                        # 更新订单的搜索全文索引向量
                        new_order.search_vector = FlatConcatSearchVector(
                            *prepare_order_search_vector_value(new_order)
                        )
                        new_order.save(update_fields=["search_vector"])

                        orders.append(new_order)
                    order.delete()

                # 不需要拆单的订单
                if len(new_order_lines) <= 1:
                    order.charge_status = OrderChargeStatus.FULL
                    order.total_charged_amount = total_amount
                    order.supplier = supplier
                    order.payment_at = timezone.now()
                    order.status = OrderStatus.PAID

                    events.append(
                        OrderEvent(order=order, type=OrderEvents.ORDER_FULLY_PAID,
                                   user=user, app=app))
                    order_lines = order.lines.all()
                    products_name = ""
                    for line in order_lines:
                        products_name += line.product_name + ","

                    products_name = products_name[:-1]

                    # 核销类，生成核销码
                    if product_type in [OrderTypeEnum.REDEMPTION,
                                        OrderTypeEnum.SELF_PICKUP]:
                        is_code_exists = True
                        code = None
                        while is_code_exists:
                            code = order_utils.generate_random_num()
                            exist = OrderLineModel.objects.filter(
                                redemption_code=code).exists()
                            if not exist:
                                is_code_exists = False
                        if code is not None:
                            for line in order_lines:  # order line只有一行
                                line.redemption_code = code
                                line.save()

                    # 其他
                    if product_type in [OrderTypeEnum.OTHER]:
                        order.status = OrderStatus.PAID
                        # events.append(
                        #     OrderEvent(order=order, type=OrderEvents.ORDER_FULLY_PAID, user=user, app=app))
                        # order.type = OrderTypeEnum.NON_REDEMPTION
                        #  系统自动默认评价队列
                        # for line in order_lines:
                        #     redis_zset = ZSet()
                        #     now = int(timezone.now().timestamp())
                        #     score = now + settings.ORDER_RECEIVED_QUEUE_DELAY_SECONDS
                        #     redis_zset.add(settings.ORDER_RECEIVED_QUEUE_KEY, str(line.id), score)

                    # 非核销类
                    if product_type in [OrderTypeEnum.NON_REDEMPTION]:
                        order.status = OrderStatus.PAID
                        # event_type = OrderEvents.ORDER_FULLY_PAID,
                        # events.append(OrderEvent(order=order, type=event_type, user=user, app=app))

                    # 自提类
                    if product_type == OrderTypeEnum.SELF_PICKUP:
                        order.status = OrderStatus.PAID

                    # 核销类
                    if product_type == OrderTypeEnum.REDEMPTION:
                        # order.status = OrderStatus.FULFILLED
                        order.status = OrderStatus.PAID

                    # # 促销类
                    # if order.is_promotion:
                    #     if order.type == OrderTypeEnum.NON_REDEMPTION:
                    #         order.status = OrderStatus.FINISHED
                    #         events.append(
                    #             OrderEvent(order=order, type=OrderEvents.FINISHED,
                    #                        user=user, app=app))

                    order.save()

                    orders = [order]
                OrderEvent.objects.bulk_create(events)
                # 支付
                datas = {
                    "appid": "wxd678efh567hg6787",
                    "mchid": "1230000109",
                    "description": "Image形象店-深圳腾大-QQ公仔",
                    "out_trade_no": "1217752501201407033233368018",
                    "time_expire": "2018-06-08T10:34:56+08:00",
                    "attach": "自定义数据说明",
                    "notify_url": " https://www.weixin.qq.com/wxpay/pay.php",
                    "goods_tag": "WXG",
                    "support_fapiao": True,
                    "amount": {
                        "total": 100,
                        "currency": "CNY"
                    },
                    "payer": {
                        "openid": "oUpF8uMuAJO_M2pxb1Q9zNjWeS6o\t"
                    },
                }
                authorization = cls.gener_author("POST",
                                                 "/v3/pay/transactions/jsapi",
                                                 json.dumps(datas,
                                                            ensure_ascii=False),
                                                 os.environ.get('mchid'),
                                                 os.environ.get('serial_no'))
                headers["Authorization"] = authorization

                response = requests.post(
                    "https://api.mch.weixin.qq.com/v3/pay/transactions/app",
                    data=json.dumps(datas, ensure_ascii=False), headers=headers)
                payment_data = response.json()
                if 'prepay_id' in payment_data.keys():
                    prepayid = payment_data['prepay_id']
                else:
                    raise ValidationError(
                        {
                            "prepay_id": ValidationError(
                                "prepay_id is vaild",
                                code=OrderErrorCode.PREPAY_ID_NOT_FOUND.value
                            )
                        }
                    )
                sign, noncestr, timestamp = cls.gener_paysign(os.environ.get('appid'),
                                                              prepayid)
                data = {"sign": sign, "noncestr": noncestr, "timestamp": timestamp}
            return OrderPayment(orders=orders, data=data)
        except Exception as e:
            logger.error(f"创建订单失败:{str(e)}")
