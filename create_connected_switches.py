import ipaddress
import uuid

from nautobot.apps.jobs import Job, ObjectVar, BooleanVar, register_jobs
from nautobot.dcim.models import Device, DeviceType, Location, Interface, Cable
from nautobot.extras.models import Status, Role
from nautobot.ipam.models import Prefix, IPAddress

class CreateSwitchPair(Job):
    """
    Creates two switches using the provided location, device type, and device role.
    The job *does not create a new interface*; instead it locates the first existing
    interface (by name) on each device, connects them with a cable, and assigns the
    first available /31 subnet from the 10.0.0.0/8 IPAM prefix to those interfaces.
    All objects (devices, IPs, prefix) are set to Active (or Connected for the Cable).

    When debug is enabled, additional debug messages are logged.
    """
    class Meta:
        name = "Create Switch Pair with /31 Subnet"
        description = (
            "Creates 2 switches using the selected location, device type, and device role. "
            "It retrieves the first existing interface (by name) from each device and connects "
            "them with a cable, then assigns the first available /31 subnet from 10.0.0.0/8. "
            "All objects are set to Active (Connected for cables). Enable debug mode for additional logging."
        )
        field_order = ["location", "device_type", "device_role", "debug"]

    # Dropdown for selecting the location.
    location = ObjectVar(
        description="Select the location for the new switches",
        model=Location,
    )

    # Dropdown for selecting the device type.
    device_type = ObjectVar(
        description="Select the device type for the new switches",
        model=DeviceType,
    )

    # Dropdown for selecting the device role.
    device_role = ObjectVar(
        description="Select the device role to assign to both switches",
        model=Role,
    )

    # Boolean input for enabling debug mode.
    debug = BooleanVar(
        description="Enable debug logging",
        default=False,
    )

    def run(self, *, location, device_type, device_role, debug):
        # Retrieve the "Active" status from Nautobot.
        active_status = Status.objects.get(name="Active")
        if debug:
            self.logger.debug("Active status retrieved: %s", active_status)

        # Generate unique names for the devices.
        device_name1 = f"switch1-{uuid.uuid4().hex[:6]}"
        device_name2 = f"switch2-{uuid.uuid4().hex[:6]}"
        if debug:
            self.logger.debug("Generated device names: %s, %s", device_name1, device_name2)

        # Create two switch devices.
        switch1 = Device(
            name=device_name1,
            device_type=device_type,
            role=device_role,
            location=location,
            status=active_status,
        )
        switch1.validated_save()
        self.logger.info("Created device", extra={"object": switch1})
        if debug:
            self.logger.debug(
                "Switch1 created with device_type=%s, role=%s, location=%s",
                device_type, device_role, location
            )

        switch2 = Device(
            name=device_name2,
            device_type=device_type,
            role=device_role,
            location=location,
            status=active_status,
        )
        switch2.validated_save()
        self.logger.info("Created device", extra={"object": switch2})
        if debug:
            self.logger.debug(
                "Switch2 created with device_type=%s, role=%s, location=%s",
                device_type, device_role, location
            )

        # Retrieve the "first" interface for each device, ordered by name.
        # Raise an error if no interface is found on a device.
        iface1 = switch1.interfaces.order_by("name").first()
        if not iface1:
            raise ValueError(f"No interface found on device {switch1.name}. Please create an interface before running this job.")

        iface2 = switch2.interfaces.order_by("name").first()
        if not iface2:
            raise ValueError(f"No interface found on device {switch2.name}. Please create an interface before running this job.")

        if debug:
            self.logger.debug("Retrieved first interface of switch1: %s", iface1)
            self.logger.debug("Retrieved first interface of switch2: %s", iface2)

        # Retrieve the Cable status ("Connected") as a Status instance.
        cable_status = Status.objects.get(name="Connected")
        if debug:
            self.logger.debug("Cable status retrieved: %s", cable_status)

        # Connect the two interfaces with a cable.
        cable = Cable(
            termination_a=iface1,
            termination_b=iface2,
            status=cable_status,
        )
        cable.validated_save()
        self.logger.info("Connected interfaces with cable", extra={"object": cable})
        if debug:
            self.logger.debug("Cable connected between %s and %s", iface1, iface2)

        # Ensure the parent IPAM prefix exists.
        parent_prefix, _ = Prefix.objects.get_or_create(
            prefix="10.0.0.0/8",
            defaults={"description": "Parent prefix for switch interconnections", "status": active_status},
        )
        if debug:
            self.logger.debug("Parent prefix ensured: %s", parent_prefix)

        # Find the first available /31 subnet within 10.0.0.0/8.
        parent_network = ipaddress.ip_network("10.0.0.0/8")
        candidate_subnet_str = None
        candidate_ip_a = None
        candidate_ip_b = None

        for candidate in parent_network.subnets(new_prefix=31):
            # A /31 subnet has exactly 2 addresses.
            ips = list(candidate)
            ip_a = f"{ips[0]}/31"
            ip_b = f"{ips[1]}/31"
            # Check if either IP is already used.
            if not IPAddress.objects.filter(address=ip_a).exists() and not IPAddress.objects.filter(address=ip_b).exists():
                candidate_subnet_str = str(candidate)
                candidate_ip_a = ip_a
                candidate_ip_b = ip_b
                if debug:
                    self.logger.debug(
                        "Found available candidate subnet: %s with IPs %s and %s",
                        candidate_subnet_str,
                        candidate_ip_a,
                        candidate_ip_b
                    )
                break

        if candidate_subnet_str is None:
            raise Exception("No available /31 subnet found in 10.0.0.0/8")

        # Get or create the Prefix for the candidate subnet.
        try:
            subnet = Prefix.objects.get(prefix=candidate_subnet_str)
            if debug:
                self.logger.debug("Found existing subnet: %s", subnet)
        except Prefix.DoesNotExist:
            subnet = Prefix(
                prefix=candidate_subnet_str,
                description="First available /31 subnet for switch interconnection",
                status=active_status,
            )
            subnet.full_clean()
            subnet.save()
            self.logger.info("Created /31 prefix", extra={"object": subnet})
            if debug:
                self.logger.debug("Created new subnet: %s", subnet)

        # Create IPAddress instances for each interface using the candidate IPs.
        ip1 = IPAddress(address=candidate_ip_a, status=active_status)
        ip1.full_clean()
        ip1.save()
        # Associate the IP address with the interface via the many-to-many "interfaces" relation.
        ip1.interfaces.add(iface1)
        self.logger.info("Assigned IP to switch1 interface", extra={"object": ip1})
        if debug:
            self.logger.debug("Assigned IP %s to interface %s", ip1.address, iface1)

        ip2 = IPAddress(address=candidate_ip_b, status=active_status)
        ip2.full_clean()
        ip2.save()
        ip2.interfaces.add(iface2)
        self.logger.info("Assigned IP to switch2 interface", extra={"object": ip2})
        if debug:
            self.logger.debug("Assigned IP %s to interface %s", ip2.address, iface2)

        # Generate a CSV summary of the new devices, interfaces, and assigned IPs.
        output_lines = ["device,interface,ip_address"]
        for switch in (switch1, switch2):
            interface = switch.interfaces.order_by("name").first()
            ip_obj = IPAddress.objects.filter(interfaces=interface).first()
            ip_str = ip_obj.address if ip_obj else "None"
            output_lines.append(f"{switch.name},{interface.name},{ip_str}")
            if debug:
                self.logger.debug(
                    "Summary entry for %s: interface %s with IP %s",
                    switch.name, interface.name, ip_str
                )

        if debug:
            self.logger.debug(
                "Job completed successfully. Output:\n%s",
                "\n".join(output_lines)
            )

        return "\n".join(output_lines)

register_jobs(CreateSwitchPair)
