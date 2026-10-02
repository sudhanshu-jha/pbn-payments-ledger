from django.contrib import admin

from .models import IdempotencyKey, LedgerEntry, Payment, WebhookEvent


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 0
    can_delete = False
    readonly_fields = tuple(f.name for f in LedgerEntry._meta.fields)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "failure_code",
        "amount",
        "currency",
        "method",
        "last4",
        "masked_reference",
        "created_at",
    )
    list_filter = ("status", "method")
    readonly_fields = (*(f.name for f in Payment._meta.fields), "masked_reference")
    inlines = (LedgerEntryInline,)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "payment",
        "sequence",
        "event_type",
        "from_status",
        "to_status",
        "failure_code",
        "created_at",
    )
    readonly_fields = tuple(f.name for f in LedgerEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "payment", "status", "outcome", "delivery_count", "received_at")
    list_filter = ("outcome", "status")
    readonly_fields = tuple(f.name for f in WebhookEvent._meta.fields)


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ("key", "response_status", "payment", "created_at")
    readonly_fields = tuple(f.name for f in IdempotencyKey._meta.fields)
