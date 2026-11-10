"""Restore the legacy FPR mapping used by existing SVG AIPs.

Older Archivematica METS files may identify a generic SVG with the FPR key
``Generic SVG``.  Keeping that key resolvable prevents reingest from losing
the format metadata merely because the current FPR no longer exposes the
historical rule.
"""

from django.db import migrations


COMMAND_OUTPUT = "Generic SVG"
FORMAT_VERSION_UUID = "8ba2101d-3f3c-4620-a0fa-a75cf5f5ec27"


def restore_generic_svg_rule(apps, schema_editor):
    IDCommand = apps.get_model("fpr", "IDCommand")
    IDRule = apps.get_model("fpr", "IDRule")
    FormatVersion = apps.get_model("fpr", "FormatVersion")

    format_version = FormatVersion.objects.get(uuid=FORMAT_VERSION_UUID)
    command = IDCommand.objects.filter(enabled=True).order_by("description").first()
    if command is None:
        return

    IDRule.objects.get_or_create(
        command=command,
        command_output=COMMAND_OUTPUT,
        defaults={"format": format_version, "enabled": True},
    )


def remove_generic_svg_rule(apps, schema_editor):
    IDRule = apps.get_model("fpr", "IDRule")
    IDRule.objects.filter(command_output=COMMAND_OUTPUT).delete()


class Migration(migrations.Migration):
    dependencies = [("fpr", "0053_update_jhove_tool")]

    operations = [migrations.RunPython(restore_generic_svg_rule, remove_generic_svg_rule)]
