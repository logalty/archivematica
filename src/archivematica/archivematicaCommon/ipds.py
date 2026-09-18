import json


def remove_object_salt(unit_variables):
    """Remove transient encryption salts while preserving other attributes."""
    for unit_var in unit_variables:
        if not unit_var.variablevalue:
            continue
        try:
            attributes = json.loads(unit_var.variablevalue)
        except json.JSONDecodeError:
            continue
        if not isinstance(attributes, dict) or "object_salt" not in attributes:
            continue

        attributes.pop("object_salt")
        if attributes:
            unit_var.variablevalue = json.dumps(attributes, sort_keys=True)
            unit_var.save(update_fields=["variablevalue"])
        else:
            unit_var.delete()
