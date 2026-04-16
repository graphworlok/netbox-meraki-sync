from utilities.choices import ChoiceSet


class SyncStatusChoices(ChoiceSet):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED  = "failed"

    CHOICES = [
        (PENDING, "Pending",  "secondary"),
        (RUNNING, "Running",  "warning"),
        (SUCCESS, "Success",  "success"),
        (FAILED,  "Failed",   "danger"),
    ]


class DeviceFamilyChoices(ChoiceSet):
    """Meraki product family codes derived from the model string prefix."""
    MS = "MS"   # Switches
    MX = "MX"   # Security appliances / SD-WAN
    MR = "MR"   # Wireless access points
    MG = "MG"   # Cellular gateways
    MV = "MV"   # Smart cameras
    MT = "MT"   # Sensors
    OTHER = "other"

    CHOICES = [
        (MS,    "Switch (MS)",              "blue"),
        (MX,    "Appliance / SD-WAN (MX)", "orange"),
        (MR,    "Access Point (MR)",        "green"),
        (MG,    "Cellular Gateway (MG)",    "purple"),
        (MV,    "Camera (MV)",              "cyan"),
        (MT,    "Sensor (MT)",              "gray"),
        (OTHER, "Other",                    "secondary"),
    ]
