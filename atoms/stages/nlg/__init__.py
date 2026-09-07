"""NLG stage package — reply generation.

Exports:
    - ``BaseNLG``: abstract base class for NLG atoms.stages.
    - ``FSMNLG``: reply generation for FSM modules.
    - ``RouteNLG``: reply generation for the top-level routing module.
"""

from atoms.stages.nlg.nlg import BaseNLG, FSMNLG, RouteNLG

__all__ = ["BaseNLG", "FSMNLG", "RouteNLG"]
