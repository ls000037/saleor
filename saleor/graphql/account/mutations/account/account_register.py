from urllib.parse import urlencode

import graphene
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError

from .....account import events as account_events
from .....account import models, notifications, search
from .....account.error_codes import AccountErrorCode
from .....core.tracing import traced_atomic_transaction
from .....core.utils.url import prepare_url, validate_storefront_url
from .....webhook.event_types import WebhookEventAsyncType
from ....channel.utils import clean_channel
from ....core import ResolveInfo
from ....core.doc_category import DOC_CATEGORY_USERS
from ....core.enums import LanguageCodeEnum
from ....core.mutations import ModelMutation, ModelWithExtRefMutation, \
    ModelDeleteMutation
from ....core.types import AccountError, NonNullList, BaseInputObjectType
from ....core.utils import WebhookEventInfo
from ....meta.inputs import MetadataInput
from ....plugins.dataloaders import get_plugin_manager_promise
from ....site.dataloaders import get_site_promise
from ...types import User, Supplier
from .base import AccountBaseInput
from .....permission.enums import AccountPermissions


class AccountRegisterInput(AccountBaseInput):
    email = graphene.String(description="The email address of the user.", required=True)
    password = graphene.String(description="Password.", required=True)
    first_name = graphene.String(description="Given name.")
    last_name = graphene.String(description="Family name.")
    redirect_url = graphene.String(
        description=(
            "Base of frontend URL that will be needed to create confirmation URL."
        ),
        required=False,
    )
    language_code = graphene.Argument(
        LanguageCodeEnum, required=False, description="User language code."
    )
    metadata = NonNullList(
        MetadataInput,
        description="User public metadata.",
        required=False,
    )
    channel = graphene.String(
        description=(
            "Slug of a channel which will be used to notify users. Optional when "
            "only one channel exists."
        )
    )

    class Meta:
        description = "Fields required to create a user."
        doc_category = DOC_CATEGORY_USERS


class AccountRegister(ModelMutation):
    class Arguments:
        input = AccountRegisterInput(
            description="Fields required to create a user.", required=True
        )

    requires_confirmation = graphene.Boolean(
        description="Informs whether users need to confirm their email address."
    )

    class Meta:
        description = "Register a new user."
        doc_category = DOC_CATEGORY_USERS
        exclude = ["password"]
        model = models.User
        object_type = User
        error_type_class = AccountError
        error_type_field = "account_errors"
        support_meta_field = True
        webhook_events_info = [
            WebhookEventInfo(
                type=WebhookEventAsyncType.CUSTOMER_CREATED,
                description="A new customer account was created.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.NOTIFY_USER,
                description="A notification for account confirmation.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ACCOUNT_CONFIRMATION_REQUESTED,
                description=(
                    "An user confirmation was requested. "
                    "This event is always sent regardless of settings."
                ),
            ),
        ]

    @classmethod
    def mutate(cls, root, info: ResolveInfo, **data):
        site = get_site_promise(info.context).get()
        response = super().mutate(root, info, **data)
        response.requires_confirmation = (
            site.settings.enable_account_confirmation_by_email
        )
        return response

    @classmethod
    def clean_input(cls, info: ResolveInfo, instance, data, **kwargs):
        site = get_site_promise(info.context).get()

        if not site.settings.enable_account_confirmation_by_email:
            return super().clean_input(info, instance, data, **kwargs)
        elif not data.get("redirect_url"):
            raise ValidationError(
                {
                    "redirect_url": ValidationError(
                        "This field is required.", code=AccountErrorCode.REQUIRED.value
                    )
                }
            )

        try:
            validate_storefront_url(data["redirect_url"])
        except ValidationError as error:
            raise ValidationError(
                {
                    "redirect_url": ValidationError(
                        error.message, code=AccountErrorCode.INVALID.value
                    )
                }
            )

        data["channel"] = clean_channel(
            data.get("channel"), error_class=AccountErrorCode
        ).slug

        data["email"] = data["email"].lower()

        password = data["password"]
        try:
            password_validation.validate_password(password, instance)
        except ValidationError as error:
            raise ValidationError({"password": error})

        data["language_code"] = data.get("language_code", settings.LANGUAGE_CODE)
        return super().clean_input(info, instance, data, **kwargs)

    @classmethod
    def save(cls, info: ResolveInfo, user, cleaned_input):
        password = cleaned_input["password"]
        user.set_password(password)
        user.search_document = search.prepare_user_search_document_value(
            user, attach_addresses_data=False
        )
        manager = get_plugin_manager_promise(info.context).get()
        site = get_site_promise(info.context).get()
        token = None
        redirect_url = cleaned_input.get("redirect_url")

        with traced_atomic_transaction():
            # 开启/关闭 注册自动确认
            user.is_confirmed = True
            if site.settings.enable_account_confirmation_by_email:
                user.save()

                # Notifications will be deprecated in the future
                token = default_token_generator.make_token(user)
                notifications.send_account_confirmation(
                    user,
                    redirect_url,
                    manager,
                    channel_slug=cleaned_input["channel"],
                    token=token,
                )
            else:
                user.save()

            if redirect_url:
                params = urlencode(
                    {
                        "email": user.email,
                        "token": token or default_token_generator.make_token(user),
                    }
                )
                redirect_url = prepare_url(params, redirect_url)

            cls.call_event(
                manager.account_confirmation_requested,
                user,
                cleaned_input["channel"],
                token,
                redirect_url,
            )

            cls.call_event(manager.customer_created, user)
        account_events.customer_account_created_event(user=user)


class SupplierUpdateInput(BaseInputObjectType):
    name = graphene.String(
        required=False,
        description=("company name of the supplier.")
    )
    tax_number = graphene.String(
        required=False,
        description=("company tax code of the supplier.")
    )
    business_scope = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )

    after_sale_name = graphene.String(
        required=False,
        description=("after sale name of the supplier.")
    )
    after_sale_phone = graphene.String(
        required=False,
        description=("after sale phone of the supplier.")
    )

    contact_name = graphene.String(
        required=False,
        description=("contact name of the supplier.")
    )
    phone = graphene.String(required=False, description=("phone of the supplier."))
    email = graphene.String(required=False, description=("email of the supplier."))

    id_front = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )
    id_behind = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )
    author_letter = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )

    legal_id = graphene.String(required=False, description=("phone of the supplier."))
    legal_id_front = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )
    legal_id_behind = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )

    bankcard = graphene.String(required=False,
                               description=("bankcard of the supplier."))
    bankname = graphene.String(required=False,
                               description=("bankname of the supplier."))
    sub_bankname = graphene.String(
        required=False,
        description=("alt bankname of the supplier.")
    )
    currency = graphene.String(required=False,
                               description=("currency of the supplier."))
    country = graphene.String(required=False, description=("contury of the supplier."))

    product_info = graphene.String(required=False,
                                   description=("product info of the supplier."))
    product_safe_report = graphene.String(required=False,
                                          description=("status the supplier."))
    business_license = graphene.String(
        required=False,
        description=("company ranges of the supplier.")
    )
    cert_qa = graphene.String(required=False, description=("status the supplier."))
    office_picture = graphene.String(required=False,
                                     description=("status the supplier."))
    is_locked = graphene.Boolean(
        description=("whether the supplier is locked to change."))
    status = graphene.String(required=False, description=("status the supplier."))
    # is_approved = graphene.Boolean(description=("whether the supplier is approvaled."))
    is_active = graphene.Boolean(description=("whether the supplier is actived."))


class SupplierCreateInput(SupplierUpdateInput):
    user = graphene.ID(
        description="ID of the account.", name="user",
        required=True
    )

    name = graphene.String(
        required=False,
        description=("company name of the supplier.")
    )
    phone = graphene.String(required=False, description=("phone of the supplier."))
    tax_number = graphene.String(
        required=False,
        description=("company tax code of the supplier.")
    )


class SupplierCreate(ModelMutation):
    class Arguments:
        input = SupplierCreateInput(
            required=True,
            description="Fields required to create a "
                        "supplier's register info."
        )

    class Meta:
        description = "Creates a new comment."
        model = models.Supplier
        object_type = Supplier
        # permissions = (AccountPermissions.MANAGE_SUPPLIERS,)
        error_type_class = AccountError

    @classmethod
    def clean_input(cls, info: ResolveInfo, instance, data, **kwargs):
        if models.Supplier.objects.filter(name=data.get("name")):
            raise ValidationError(
                {
                    "name": ValidationError(
                        "This supplier name is existed.",
                        code=AccountErrorCode.UNIQUE.value
                    )
                }
            )
        if models.Supplier.objects.filter(tax_number=data.get("tax_number")):
            raise ValidationError(
                {
                    "tax_number": ValidationError(
                        "This supplier tax number is existed.",
                        code=AccountErrorCode.UNIQUE.value
                    )
                }
            )
        if models.Supplier.objects.filter(phone=data.get("phone")):
            raise ValidationError(
                {
                    "phone": ValidationError(
                        "This supplier phone is existed.",
                        code=AccountErrorCode.UNIQUE.value
                    )
                }
            )
    # @classmethod
    # def perform_mutation(  # type: ignore[override]
    #         cls, _root, info: ResolveInfo, /, input,
    # ):
    #     instance = cls.get_instance(info, **input)
    #     # cleaned_input 会把文件字段解析成数据对象；使用cleaned_input则不需要使用info.context.FILES.get
    #     cleaned_input = cls.clean_input(info, instance, input)
    #     # content_data = info.context.FILES.get(cleaned_input["product_safe_report"])
    #     # cleaned_input["product_safe_report"] = content_data
    #     metadata_list = cleaned_input.pop("metadata", None)
    #     private_metadata_list = cleaned_input.pop("private_metadata", None)
    #     instance = cls.construct_instance(instance, cleaned_input)
    #     cls.validate_and_update_metadata(
    #         instance, metadata_list, private_metadata_list
    #     )
    #     cls.clean_instance(info, instance)
    #     cls.save(info, instance, cleaned_input)
    #     return cls.success_response(instance)


class SupplierUpdate(SupplierCreate, ModelWithExtRefMutation):
    class Arguments:
        id = graphene.ID(required=False, description="ID of a supplier info to update.")
        external_reference = graphene.String(
            required=False,
            description=f"External ID of a supplier "
                        f"to update.", )
        input = SupplierUpdateInput(
            required=True,
            description="Fields required to update a "
                        "supplier's register info."
        )

    class Meta:
        description = "updates a supplier info."
        model = models.Supplier
        object_type = Supplier
        permissions = (AccountPermissions.MANAGE_SUPPLIERS,)
        error_type_class = AccountError


class SupplierDelete(ModelDeleteMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of a supplier to delete.")

    class Meta:
        description = "Delete a supplier."
        model = models.Supplier
        object_type = Supplier
        permissions = (AccountPermissions.MANAGE_SUPPLIERS,)
        error_type_class = AccountError
