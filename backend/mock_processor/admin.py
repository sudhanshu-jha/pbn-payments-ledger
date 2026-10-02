from django.contrib import admin

from .models import ProcessorCharge, ProcessorToken


@admin.register(ProcessorToken)
class ProcessorTokenAdmin(admin.ModelAdmin):
    list_display = ("token", "method", "last4", "brand_or_bank_type", "final_status", "used_at")
    readonly_fields = tuple(f.name for f in ProcessorToken._meta.fields)


@admin.register(ProcessorCharge)
class ProcessorChargeAdmin(admin.ModelAdmin):
    list_display = ("processor_reference", "amount", "currency", "final_status", "created_at")
    readonly_fields = tuple(f.name for f in ProcessorCharge._meta.fields)
