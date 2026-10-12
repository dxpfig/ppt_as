"""Safe PPTD YAML core scalars shared by browser service and exporter."""
import copy
import re
import json
from pathlib import Path
import yaml

class Yaml12SafeLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 core scalar rules used by this probe."""


Yaml12SafeLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for initial, rules in Yaml12SafeLoader.yaml_implicit_resolvers.items():
    Yaml12SafeLoader.yaml_implicit_resolvers[initial] = [
        (tag, pattern) for tag, pattern in rules
        if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:timestamp"}
    ]
Yaml12SafeLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
Yaml12SafeLoader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"), list("-+0123456789"))
Yaml12SafeLoader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(r"^[-+]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[0-9]+[eE][-+]?[0-9]+|\.inf|\.Inf|\.INF|\.nan|\.NaN|\.NAN)$"), list("-+0123456789."))


def construct_int(loader, node):
    value = loader.construct_scalar(node)
    sign = -1 if value.startswith("-") else 1
    unsigned = value.lstrip("+-")
    base = 8 if unsigned.startswith("0o") else 16 if unsigned.startswith("0x") else 10
    return sign * int(unsigned[2:] if base != 10 else unsigned, base)


Yaml12SafeLoader.add_constructor("tag:yaml.org,2002:int", construct_int)


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


Yaml12SafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


class ProbeDumper(yaml.SafeDumper):
    pass


def string_representer(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ProbeDumper.add_representer(str, string_representer)


def load_yaml(path: Path):
    source = path.read_text(encoding="utf-8")
    # Aliases can make different element paths share a mutable content object.
    # This limited editor rejects them rather than changing an unselected node.
    if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(source)):
        raise ValueError("YAML aliases are not supported by this disposable editing probe")
    value = yaml.load(source, Loader=Yaml12SafeLoader)
    if not isinstance(value, dict):
        raise ValueError(f"Expected YAML mapping: {path.name}")
    # The browser's JSON response is derived and must be representable without loss.
    json.dumps(value, allow_nan=False)
    return value

