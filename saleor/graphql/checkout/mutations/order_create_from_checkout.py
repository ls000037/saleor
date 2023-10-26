import graphene
from django.core.exceptions import ValidationError

from ....checkout.checkout_cleaner import validate_checkout
from ....checkout.complete_checkout import create_order_from_checkout, \
    create_order_from_checkout_new
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines, \
    retrieve_selected_checkout_items
from ....core.exceptions import GiftCardNotApplicable, InsufficientStock
from ....discount.models import NotApplicable
from ....permission.enums import CheckoutPermissions
from ....webhook.event_types import WebhookEventAsyncType, WebhookEventSyncType
from ...app.dataloaders import get_app_promise
from ...core import ResolveInfo
from ...core.descriptions import ADDED_IN_32, ADDED_IN_38
from ...core.doc_category import DOC_CATEGORY_ORDERS
from ...core.mutations import BaseMutation
from ...core.types import Error, NonNullList, BaseInputObjectType
from ...core.utils import CHECKOUT_CALCULATE_TAXES_MESSAGE, WebhookEventInfo
from ...meta.inputs import MetadataInput
from ...order.types import Order
from saleor.order import OrderType
from ...plugins.dataloaders import get_plugin_manager_promise
from ..enums import OrderCreateFromCheckoutErrorCode
from ..types import Checkout
from ..utils import prepare_insufficient_stock_checkout_validation_error
from saleor.product import models as product_models
from ...product.types import ProductVariant
from saleor.product.utils import product_type as product_type_utils
from saleor.checkout import CheckoutLineKey


class OrderCreateFromCheckoutError(Error):
    code = OrderCreateFromCheckoutErrorCode(
        description="The error code.", required=True
    )
    variants = graphene.List(
        graphene.NonNull(graphene.ID),
        description="List of variant IDs which causes the error.",
        required=False,
    )
    lines = graphene.List(
        graphene.NonNull(graphene.ID),
        description="List of line Ids which cause the error.",
        required=False,
    )

    class Meta:
        doc_category = DOC_CATEGORY_ORDERS


class CreateUnpaidOrderLineInput(BaseInputObjectType):
    quantity = graphene.Int(required=True, description="The number of items purchased.")
    variant_id = graphene.ID(required=True, description="ID of the product variant.")
    # is_promotion = graphene.Boolean(description="是否是促销订单", required=False)


class OrderCreateFromCheckout(BaseMutation):
    order = graphene.Field(Order, description="Placed order.")

    class Arguments:
        id = graphene.ID(
            required=True,
            description="ID of a checkout that will be converted to an order.",
        )
        lines = NonNullList(
            CreateUnpaidOrderLineInput,
            description=(
                "A list of checkout lines, each containing information about "
                "an item in the checkout."
            ),
            required=False,
        )
        remove_checkout = graphene.Boolean(
            description=(
                "Determines if checkout should be removed after creating an order. "
                "Default true."
            ),
            default_value=True,
        )
        private_metadata = NonNullList(
            MetadataInput,
            description=(
                "Fields required to update the checkout private metadata." + ADDED_IN_38
            ),
            required=False,
        )
        metadata = NonNullList(
            MetadataInput,
            description=(
                "Fields required to update the checkout metadata." + ADDED_IN_38
            ),
            required=False,
        )

    class Meta:
        auto_permission_message = False
        description = (
            "Create new order from existing checkout. Requires the "
            "following permissions: AUTHENTICATED_APP and HANDLE_CHECKOUTS."
            + ADDED_IN_32
        )
        doc_category = DOC_CATEGORY_ORDERS
        object_type = Order
        # permissions = (CheckoutPermissions.HANDLE_CHECKOUTS,)
        error_type_class = OrderCreateFromCheckoutError
        support_meta_field = True
        support_private_meta_field = True
        webhook_events_info = [
            WebhookEventInfo(
                type=WebhookEventSyncType.SHIPPING_LIST_METHODS_FOR_CHECKOUT,
                description=(
                    "Optionally triggered when cached external shipping methods are "
                    "invalid."
                ),
            ),
            WebhookEventInfo(
                type=WebhookEventSyncType.CHECKOUT_FILTER_SHIPPING_METHODS,
                description=(
                    "Optionally triggered when cached filtered shipping methods are "
                    "invalid."
                ),
            ),
            WebhookEventInfo(
                type=WebhookEventSyncType.CHECKOUT_CALCULATE_TAXES,
                description=CHECKOUT_CALCULATE_TAXES_MESSAGE,
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ORDER_CREATED,
                description="Triggered when order is created.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.NOTIFY_USER,
                description="A notification for order placement.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.NOTIFY_USER,
                description="A staff notification for order placement.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ORDER_UPDATED,
                description=(
                    "Triggered when order received the update after placement."
                ),
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ORDER_PAID,
                description="Triggered when newly created order is paid.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ORDER_FULLY_PAID,
                description="Triggered when newly created order is fully paid.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ORDER_CONFIRMED,
                description=(
                    "Optionally triggered when newly created order are automatically "
                    "marked as confirmed."
                ),
            ),
        ]

    @classmethod
    def validate_lines(cls, lines):
        if not lines:
            raise ValidationError(
                {
                    "lines": ValidationError(
                        "Order must contain at least one line.",
                        code=OrderCreateFromCheckoutErrorCode.REQUIRED.value,
                    )
                }
            )
        for item in lines:
            if "quantity" not in item or "variant_id" not in item:
                raise ValidationError(
                    {
                        "lines": ValidationError(
                            "The lines argument must include quantity and variant_id fields.",
                            code=OrderCreateFromCheckoutErrorCode.REQUIRED.value,
                        )
                    }
                )

            quantity = item["quantity"]
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError(
                    {
                        "lines": ValidationError(
                            "The quantity in a line must be a positive integer.",
                            code=OrderCreateFromCheckoutErrorCode.INVALID_QUANTITY.value,
                        )
                    }
                )

    @classmethod
    def get_variant_ids(cls, lines):
        return [item["variant_id"] for item in lines]

    # @classmethod
    # def check_permissions(cls, context, permissions=None, **data):
    #     """Determine whether app has rights to perform this mutation."""
    #     permissions = permissions or cls._meta.permissions
    #     app = getattr(context, "app", None)
    #     if app:
    #         return app.has_perms(permissions)
    #     return False

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls,
        _root,
        info: ResolveInfo,
        /,
        *,
        id,
        lines,
        # is_promotion=False,
        metadata=None,
        private_metadata=None,
        remove_checkout,
    ):
        user = info.context.user
        cls.validate_lines(lines)

        checkout = cls.get_node_or_error(
            info,
            id,
            field="id",
            only_type=Checkout,
            code=OrderCreateFromCheckoutErrorCode.CHECKOUT_NOT_FOUND.value,
        )
        _checkout_lines = checkout.lines.all()
        if cls._meta.support_meta_field and metadata is not None:
            cls.check_metadata_permissions(info, id)
            cls.validate_metadata_keys(metadata)
        if cls._meta.support_private_meta_field and private_metadata is not None:
            cls.check_metadata_permissions(info, id, private=True)
            cls.validate_metadata_keys(private_metadata)

        manager = get_plugin_manager_promise(info.context).get()
        # 获取lines的variant_id
        variant_ids = cls.get_variant_ids(lines)
        variants = cls.get_nodes_or_error(
            variant_ids,
            "variant_id",
            ProductVariant,
            qs=product_models.ProductVariant.objects.prefetch_related(
                "product__product_type"
            ),
        )
        supplier_variant_lines = {}
        # order_type = None
        # supplier = None
        # product_promotion = None
        # quantity = 0
        for variant, line in zip(variants, lines):
            print(line, "0000000")
            product_type = product_type_utils.get_product_type(
                variant.product.product_type)
            if product_type is None:
                raise ValidationError(
                    {
                        "productType": ValidationError(
                            # 不支持的订单类型
                            "Unsupported order type.",
                            code=OrderCreateFromCheckoutErrorCode.INVALID_PRODUCT_TYPE.value,
                        )
                    }
                )

            if len(lines) > 1:
                if product_type in [OrderType.NON_REDEMPTION, OrderType.REDEMPTION,
                                    OrderType.INTEGRAL,
                                    OrderType.OTHER, OrderType.CHECKUP,
                                    OrderType.CHECKUP_MANAGER,
                                    OrderType.SELF_PICKUP]:
                    raise ValidationError(
                        {
                            "variant": ValidationError(
                                "Includes order types where only one item can be purchased.",
                                code=OrderCreateFromCheckoutErrorCode.EXCEEDS_MAXIMUM_VARIANT_QUANTITY.value,
                            )
                        }
                    )

            supplier = variant.product.supplier
            if supplier is None:
                raise ValidationError(
                    {
                        "supplier": ValidationError(
                            "Product must belong to a supplier.",
                            code=OrderCreateFromCheckoutErrorCode.INVALID_SUPPLIER.value,
                        )
                    }
                )
            product_variant_channel_list = product_models.ProductVariantChannelListing.objects.get(
                variant_id=variant.id, channel_id=checkout.channel_id
            )
            # if is_promotion:
            #     price_amount = product_variant_channel_list.discount_amount
            # else:
            price_amount = product_variant_channel_list.price_amount

            variant_line = {CheckoutLineKey.VARIANT_KEY: variant,
                            # 数量
                            CheckoutLineKey.QUANTITY_KEY: line["quantity"],
                            # 单价
                            CheckoutLineKey.UNIT_PRICE_KEY: price_amount,
                            # 类型
                            # CheckoutLineKey.PRODUCT_TYPE_KEY: product_type,
                            # 优惠券列表
                            # CheckoutLineKey.VOUCHERS_KEY: [],
                            # 原订单总结
                            # CheckoutLineKey.SUPPLIER_TOTAL_PRICE_KEY: 0,
                            # # order_line供应商优惠金额
                            # CheckoutLineKey.SUPPLIER_DISCOUNT_LINE_VALUE_KEY: 0,
                            # # order_line平台优惠金额
                            # CheckoutLineKey.PLATFORM_DISCOUNT_LINE_VALUE_KEY: 0,
                            # # 供应商订单总优惠
                            # CheckoutLineKey.SUPPLIER_DISCOUNT_TOTAL_VALUE_KEY: 0,
                            # # 平台订单总优惠
                            # CheckoutLineKey.PLATFORM_DISCOUNT_TOTAL_VALUE_KEY: 0,
                            # # 优惠后订单总价
                            # CheckoutLineKey.AFTER_DISCOUNT_TOTAL_PRICE_KEY: 0,
                            # 优惠后单价
                            CheckoutLineKey.UNIT_AFTER_DISCOUNT_PRICE_KEY: price_amount,
                            # # 供应商优惠券
                            # CheckoutLineKey.SUPPLIER_VOUCHER_APPLY_KEY: None,
                            # # 平台优惠券
                            # CheckoutLineKey.PLATFORM_VOUCHER_APPLY_KEY: None,
                            }
            # order_type = product_type

            if supplier.id not in supplier_variant_lines:
                supplier_variant_lines[supplier.id] = []
            supplier_variant_lines[supplier.id].append(variant_line)
        checkout_lines, unavailable_variant_pks = retrieve_selected_checkout_items(
            supplier_variant_lines,
            checkout)
        # checkout_lines, unavailable_variant_pks = fetch_checkout_lines(checkout)
        if not checkout_lines:
            raise ValidationError(
                {
                    "lines": ValidationError(
                        # 购物车中没有该商品
                        "The product(s) is not in the shopping cart.",
                        code=OrderCreateFromCheckoutErrorCode.INVALID_ARGUMENT.value,
                    )
                }
            )
        checkout_info = fetch_checkout_info(checkout, checkout_lines, manager)
        validate_checkout(
            checkout_info=checkout_info,
            lines=checkout_lines,
            unavailable_variant_pks=unavailable_variant_pks,
            manager=manager,
        )
        app = get_app_promise(info.context).get()
        try:
            order = create_order_from_checkout_new(
                checkout_info=checkout_info,
                checkout_lines=checkout_lines,
                manager=manager,
                user=user,
                app=app,
                delete_checkout=remove_checkout,
                metadata_list=metadata,
                # order_type=order_type,
                private_metadata_list=private_metadata,
            )
        except NotApplicable:
            code = OrderCreateFromCheckoutErrorCode.VOUCHER_NOT_APPLICABLE.value
            raise ValidationError(
                {
                    "voucher_code": ValidationError(
                        "Voucher not applicable",
                        code=code,
                    )
                }
            )
        except InsufficientStock as e:
            error = prepare_insufficient_stock_checkout_validation_error(e)
            raise error
        except GiftCardNotApplicable as e:
            raise ValidationError({"gift_cards": e})
        return OrderCreateFromCheckout(order=order)
