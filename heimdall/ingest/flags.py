"""Pixel-flag vocabulary and masking policy.

The vocabulary is read from the product itself, never assumed. It supports two header conventions:

- QR2 (pipeline 6.x): ``MP_<NAME> = <bit>``.
- QR3 (pipeline 7.x): ``MSKNnnnn = <NAME>``, ``MSKMnnnn = 1 << nnnn`` and ``MSKDnnnn = <description>``; QR3
  also defines CROSSTALK, GHOST*, STREAK, BLOOM, SNOWBALL and HALO, which QR2 headers do not.

Bit numbers agree between the two releases for every shared name. A QR3 plane whose mask is not 1 << n is
ignored and reported. Flag names that a product does not define are reported, never assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class FlagVocabulary:
    bits: dict[str, int]  # name -> bit number
    convention: str = ""
    descriptions: dict[str, str] = field(default_factory=dict)
    inconsistent: tuple[str, ...] = ()

    @classmethod
    def from_header(cls, header) -> FlagVocabulary:
        bits: dict[str, int] = {}
        descriptions: dict[str, str] = {}
        conventions, inconsistent = [], []
        for card in header.cards:
            key = card.keyword
            if key.startswith("MP_"):
                value = card.value
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 63:
                    bits[key[3:]] = value
                    descriptions[key[3:]] = str(card.comment)
                    if "MP_<NAME>" not in conventions:
                        conventions.append("MP_<NAME>")
        for card in header.cards:
            m = re.fullmatch(r"MSKN(\d{4})", card.keyword)
            if not m:
                continue
            n = int(m.group(1))
            name = str(card.value).strip()
            mask = header.get(f"MSKM{m.group(1)}")
            if not name or not isinstance(mask, int) or isinstance(mask, bool) or mask != (1 << n) or n >= 63:
                inconsistent.append(f"MSKN{m.group(1)}={name!r} (mask {mask!r})")
                continue
            if name in bits and bits[name] != n:
                inconsistent.append(f"{name}: MP_ bit {bits[name]} differs from MSK plane {n}")
                continue
            bits[name] = n
            descriptions.setdefault(name, str(header.get(f"MSKD{m.group(1)}", "")).strip())
            if "MSKN/MSKM" not in conventions:
                conventions.append("MSKN/MSKM")
        return cls(bits=bits, convention="+".join(conventions), descriptions=descriptions, inconsistent=tuple(inconsistent))

    @property
    def defined(self) -> bool:
        return bool(self.bits)

    def mask_for(self, names) -> tuple[int, tuple[str, ...]]:
        """Return the combined bit mask and the names not defined by this product."""
        mask = 0
        missing = []
        for name in names:
            bit = self.bits.get(name)
            if bit is None:
                missing.append(name)
            else:
                mask |= 1 << bit
        return mask, tuple(missing)

    def names_in(self, value: int) -> tuple[str, ...]:
        return tuple(sorted(name for name, bit in self.bits.items() if value & (1 << bit)))


@dataclass(frozen=True)
class FlagMasks:
    exclude: int
    warning: int
    background_exclude: int
    undefined: tuple[str, ...]

    def bad_pixels(self, flags: np.ndarray) -> np.ndarray:
        return (flags & self.exclude) != 0

    def warning_pixels(self, flags: np.ndarray) -> np.ndarray:
        return (flags & self.warning) != 0

    def background_excluded(self, flags: np.ndarray) -> np.ndarray:
        return (flags & self.background_exclude) != 0


def build_masks(vocabulary: FlagVocabulary, exclude, warning, background_exclude) -> FlagMasks:
    e, m1 = vocabulary.mask_for(exclude)
    w, m2 = vocabulary.mask_for(warning)
    b, m3 = vocabulary.mask_for(background_exclude)
    return FlagMasks(exclude=e, warning=w, background_exclude=b, undefined=tuple(sorted(set(m1 + m2 + m3))))
