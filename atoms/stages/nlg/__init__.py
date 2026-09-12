"""NLG stage package — reply generation.

Exports:
    - ``BaseNLG``: abstract base class for NLG atoms.stages.
    - ``FSMNLG``: reply generation for FSM patterns.
"""

from atoms.stages.nlg.nlg import BaseNLG, FSMNLG

__all__ = ["BaseNLG", "FSMNLG"]
