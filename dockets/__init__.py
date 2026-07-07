"""Docket engines — one module per layout type, one uniform interface.

Every engine exposes::

    def generate(doc, ctx, docket_cfg, out) -> DocketResult

where ``doc`` is the source DXF document, ``ctx`` the shared pipeline context
(:class:`dockets.base.Ctx`), ``docket_cfg`` the docket's canonical config
section, and ``out`` the output context (:class:`dockets.base.Out`).
"""
from . import (  # noqa: F401
    flooring,
    speakers,
    sprinklers,
    partition,
    electrical,
    finishes,
    skirting,
)

ENGINES = {
    "flooring": flooring.generate,
    "speakers": speakers.generate,
    "sprinklers": sprinklers.generate,
    "partition_plan": partition.generate,
    "electrical": electrical.generate,
    "finishes": finishes.generate,
    "skirting": skirting.generate,
}
