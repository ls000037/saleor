import logging

logger = logging.getLogger(__name__)


class AddressType:
    BILLING = "billing"
    SHIPPING = "shipping"

    CHOICES = [
        (BILLING, "Billing"),
        (SHIPPING, "Shipping"),
    ]


class CheckoutChargeStatus:
    """Determine the current charge status for the checkout.

    The checkout is considered overcharged when the sum of the transactionItem's charge
    amounts exceeds the value of `checkout.total`.
    If the sum of the transactionItem's charge amounts equals
    `checkout.total`, we consider the checkout to be fully charged.
    If the sum of the transactionItem's charge amounts covers a part of the
    `checkout.total`, we treat the checkout as partially charged.


    NONE - the funds are not charged.
    PARTIAL - the funds that are charged don't cover the checkout's total
    FULL - the funds that are charged fully cover the checkout's total
    OVERCHARGED - the charged funds are bigger than checkout's total
    """

    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"
    OVERCHARGED = "overcharged"

    CHOICES = [
        (NONE, "The checkout is not charged"),
        (PARTIAL, "The checkout is partially charged"),
        (FULL, "The checkout is fully charged"),
        (OVERCHARGED, "The checkout is overcharged"),
    ]


class CheckoutAuthorizeStatus:
    """Determine a current authorize status for checkout.

    We treat the checkout as fully authorized when the sum of authorized and charged
    funds cover the checkout.total.
    We treat the checkout as partially authorized when the sum of authorized and charged
    funds covers only part of the checkout.total
    We treat the checkout as not authorized when the sum of authorized and charged funds
    is 0.

    NONE - the funds are not authorized
    PARTIAL - the cover funds don't cover fully the checkout's total
    FULL - the cover funds covers the checkout's total
    """

    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"

    CHOICES = [
        (NONE, "The checkout is not authorized"),
        (PARTIAL, "The checkout is partially authorized"),
        (FULL, "The checkout is fully authorized"),
    ]

class CheckoutLineKey(object):
    VARIANT_KEY = "variant"  # 商品
    QUANTITY_KEY = "quantity"  # 数量
    UNIT_PRICE_KEY = "unit_price"  # 单价
    PRODUCT_TYPE_KEY = "product_type"  # 商品类型
    VOUCHERS_KEY = "vouchers"  # 优惠券列表
    SUPPLIER_TOTAL_PRICE_KEY = "supplier_total_price"  # 原订单总结
    SUPPLIER_DISCOUNT_LINE_VALUE_KEY = "supplier_discount_line_value"  # order_line供应商优惠金额
    PLATFORM_DISCOUNT_LINE_VALUE_KEY = "platform_discount_line_value"  # order_line平台优惠金额
    SUPPLIER_DISCOUNT_TOTAL_VALUE_KEY = "supplier_discount_total_value"  # 供应商订单总优惠
    PLATFORM_DISCOUNT_TOTAL_VALUE_KEY = "platform_discount_total_value"  # 平台订单总优惠
    AFTER_DISCOUNT_TOTAL_PRICE_KEY = "after_discount_total_price"  # 优惠后订单总价
    UNIT_AFTER_DISCOUNT_PRICE_KEY = "unit_after_discount_price"  # 优惠后单价
    SUPPLIER_VOUCHER_APPLY_KEY = "supplier_voucher_apply"  # 供应商是否参与优惠
    PLATFORM_VOUCHER_APPLY_KEY = "platform_voucher_apply"  # 平台是否参与优惠
