from rest_framework import serializers

from .models import LedgerEntry, Payment


class CreatePaymentSerializer(serializers.Serializer):
    """The only fields the payments API accepts. Card/bank data has no place here:
    unknown fields are rejected so a client can't accidentally post a PAN to us.
    """

    amount = serializers.IntegerField(min_value=1, help_text="Integer minor units (cents).")
    currency = serializers.ChoiceField(choices=["USD"])
    payment_token = serializers.CharField(max_length=64)

    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["unknown field"] for field in sorted(unknown)}
            )
        return attrs


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerEntry
        fields = (
            "sequence",
            "event_type",
            "from_status",
            "to_status",
            "failure_code",
            "amount_delta",
            "occurred_at",
            "created_at",
        )


class PaymentSerializer(serializers.ModelSerializer):
    processor_reference = serializers.CharField(source="masked_reference", read_only=True)
    ledger = LedgerEntrySerializer(source="ledger_entries", many=True, read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id",
            "status",
            "failure_code",
            "amount",
            "currency",
            "method",
            "last4",
            "brand_or_bank_type",
            "processor_reference",
            "created_at",
            "updated_at",
            "ledger",
        )
