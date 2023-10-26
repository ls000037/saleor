from saleor.product import models as product_models, ProductTypeKind
from saleor.order import OrderType


# 获取产品类型， 如果不是核销类，虚拟类，物流类，返回None
def get_product_type(product_type: product_models.ProductType) -> OrderType:
    if product_type.kind == ProductTypeKind.GIFT_CARD:  # 积分补差
        return OrderType.INTEGRAL
    elif product_type.kind == ProductTypeKind.OTHER:  # 其他
        return OrderType.NON_REDEMPTION
    elif product_type.kind == ProductTypeKind.NORMAL and product_type.is_digital and not product_type.is_shipping_required:  # 核销类
        return OrderType.REDEMPTION
    elif product_type.kind == ProductTypeKind.NORMAL and not product_type.is_digital and product_type.is_shipping_required:  # 物流类
        return OrderType.LOGISTICS
    # elif product_type.kind == ProductTypeKind.CHECKUP and product_type.is_manager:  # 高管体检类
    #     return OrderType.CHECKUP_MANAGER
    # elif product_type.kind == ProductTypeKind.CHECKUP and not product_type.is_manager:  # 普通体检类
    #     return OrderType.CHECKUP
    elif product_type.kind == ProductTypeKind.NORMAL and not product_type.is_digital and not product_type.is_shipping_required:  # 自提类
        return OrderType.SELF_PICKUP
    return None
