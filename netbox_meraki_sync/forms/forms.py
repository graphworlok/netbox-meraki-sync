from django import forms

from ..choices import SyncStatusChoices


class SyncLogFilterForm(forms.Form):
    """Simple filter form for the SyncLog list view."""

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"placeholder": "Network ID or name…"}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status")] + list(SyncStatusChoices),
        label="Status",
    )
