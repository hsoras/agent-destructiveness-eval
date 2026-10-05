"""Register models from the pilot's pricing snapshot before starting Inspect.

Inspect applies cost overrides before importing task modules and requires every
priced model to exist in its metadata catalog, which can lag provider catalogs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from inspect_ai.model import ModelCost, ModelInfo, get_model_info, set_model_info


def register_priced_models(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--model-cost-config", type=Path)
    options, _ = parser.parse_known_args(arguments)
    if options.model_cost_config is None:
        return
    costs = yaml.safe_load(options.model_cost_config.read_text(encoding="utf-8"))
    for model, cost in (costs or {}).items():
        if get_model_info(model) is None:
            # Leave unknown token limits unset; the snapshot only defines prices.
            set_model_info(model, ModelInfo(cost=ModelCost(**cost)))


def main() -> None:
    register_priced_models(sys.argv[1:])
    from inspect_ai._cli.main import main as inspect_main

    inspect_main()


if __name__ == "__main__":
    main()
