"""Write and read Apple's legacy typedstream archives (NSArchiver format).

Messages.app stores most message text in `message.attributedBody` as an
NSAttributedString archived this way. The writer covers the classes Messages
uses there (NSAttributedString, NSString, NSDictionary, NSNumber and their
mutable forms) and matches Foundation's NSArchiver byte for byte for them;
tests/test-synthetic-messages.py checks that against archives Foundation
produced.

Two readers: read_attributed_string() is strict and raises on anything it
doesn't know (tests use it), and decode_body() is what a message reader
should use. Its text always comes first in the archive, so decode_body()
returns the text even when an attribute uses a class it can't parse, and says
the runs are incomplete.

Format notes (from public write-ups and open-source decoders):

- Header: version 4, the string "streamtyped", system version 1000.
- Integers are one signed byte, or 0x81 + int16 LE, or 0x82 + int32 LE.
- 0x84 starts a new object, class or shared string; 0x85 is nil; 0x86 ends an
  object. 0x92 + n refers back to shared string n or object n (two tables).
- Each encodeValueOfObjCType: call writes its type string (shared), then the
  values. Classes are written as name + version, then the superclass chain,
  ending in nil.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

NEW, NIL, END = 0x84, 0x85, 0x86
INT16, INT32, FLOAT = 0x81, 0x82, 0x83
REF_BASE = 0x92
SYSTEM_VERSION = 1000

# Class chains as NSArchiver writes them: (name, version), most derived first.
_CLASSES = {
    "NSAttributedString": [("NSAttributedString", 0), ("NSObject", 0)],
    "NSString": [("NSString", 1), ("NSObject", 0)],
    "NSDictionary": [("NSDictionary", 0), ("NSObject", 0)],
    "NSNumber": [("NSNumber", 0), ("NSValue", 0), ("NSObject", 0)],
    "NSMutableAttributedString": [("NSMutableAttributedString", 0), ("NSAttributedString", 0),
                                  ("NSObject", 0)],
    "NSMutableString": [("NSMutableString", 1), ("NSString", 1), ("NSObject", 0)],
}


@dataclass(frozen=True)
class Int32:
    """An NSNumber holding a C int (objCType "i"), as Messages stores them."""
    value: int


Attrs = dict[str, "str | Int32"]


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, which is how NSString counts."""
    return len(text.encode("utf-16-le")) // 2


class _Writer:
    def __init__(self) -> None:
        self.out = bytearray()
        self.strings: dict[bytes, int] = {}
        self.objects = 0
        self.classes: dict[str, int] = {}
        self.leaves: dict[tuple[str, object], int] = {}

    def int(self, n: int) -> None:
        if -110 < n < 128:
            self.out += struct.pack("<b", n)
        elif -0x8000 <= n < 0x8000:
            self.out += bytes([INT16]) + struct.pack("<h", n)
        else:
            self.out += bytes([INT32]) + struct.pack("<i", n)

    def ref(self, index: int) -> None:
        if index >= 0x100 - REF_BASE:
            raise ValueError("archive too large for one-byte references")
        self.out.append(REF_BASE + index)

    def shared(self, s: bytes) -> None:
        if s in self.strings:
            self.ref(self.strings[s])
            return
        self.out.append(NEW)
        self.int(len(s))
        self.out += s
        self.strings[s] = len(self.strings)

    def new_object(self) -> None:
        self.out.append(NEW)
        self.objects += 1

    def cls(self, chain: list[tuple[str, int]]) -> None:
        name, version = chain[0]
        if name in self.classes:
            self.ref(self.classes[name])
            return
        self.out.append(NEW)
        self.shared(name.encode())
        self.int(version)
        self.classes[name] = self.objects
        self.objects += 1
        if len(chain) > 1:
            self.cls(chain[1:])
        else:
            self.out.append(NIL)

    def leaf(self, key: tuple[str, object]) -> bool:
        """Back-reference an equal NSString/NSNumber already written."""
        if key in self.leaves:
            self.ref(self.leaves[key])
            return True
        self.leaves[key] = self.objects
        return False

    def string(self, text: str, cls: str = "NSString") -> None:
        self.shared(b"@")
        if self.leaf(("s", text)):
            return
        self.new_object()
        self.cls(_CLASSES[cls])
        data = text.encode("utf-8")
        self.shared(b"+")
        self.int(len(data))
        self.out += data
        self.out.append(END)

    def number(self, n: Int32) -> None:
        self.shared(b"@")
        if self.leaf(("i", n.value)):
            return
        self.new_object()
        self.cls(_CLASSES["NSNumber"])
        self.shared(b"*")
        self.out.append(NEW)
        self.shared(b"i")
        self.shared(b"i")
        self.int(n.value)
        self.out.append(END)

    def dictionary(self, attrs: Attrs) -> None:
        self.shared(b"@")
        self.new_object()
        self.cls(_CLASSES["NSDictionary"])
        self.shared(b"i")
        self.int(len(attrs))
        for key, value in attrs.items():
            self.string(key)
            if isinstance(value, Int32):
                self.number(value)
            else:
                self.string(value)
        self.out.append(END)


def archive_attributed_string(text: str, runs: list[tuple[int, Attrs]], *, mutable: bool = False) -> bytes:
    """Archive text with attribute runs [(length in UTF-16 units, attrs), ...].

    mutable=True archives an NSMutableAttributedString holding an
    NSMutableString, which Messages also writes."""
    if sum(length for length, _ in runs) != utf16_len(text):
        raise ValueError("run lengths must add up to the text's UTF-16 length")
    w = _Writer()
    w.out += b"\x04\x0bstreamtyped"
    w.int(SYSTEM_VERSION)
    w.shared(b"@")
    w.new_object()
    w.cls(_CLASSES["NSMutableAttributedString" if mutable else "NSAttributedString"])
    w.string(text, "NSMutableString" if mutable else "NSString")
    for ordinal, (length, attrs) in enumerate(runs, start=1):
        w.shared(b"iI")
        w.int(ordinal)
        w.int(length)
        w.dictionary(attrs)
    w.out.append(END)
    return bytes(w.out)


# --- Readers ----------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.strings: list[bytes] = []
        self.objects: list[object] = []

    def byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def int(self, head: int | None = None) -> int:
        head = self.byte() if head is None else head
        if head == INT16:
            (n,) = struct.unpack_from("<h", self.data, self.pos)
            self.pos += 2
            return n
        if head == INT32:
            (n,) = struct.unpack_from("<i", self.data, self.pos)
            self.pos += 4
            return n
        return struct.unpack("<b", bytes([head]))[0]

    def raw(self, n: int) -> bytes:
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def shared(self) -> bytes | None:
        head = self.byte()
        if head == NIL:
            return None
        if head == NEW:
            s = self.raw(self.int())
            self.strings.append(s)
            return s
        return self.strings[head - REF_BASE]

    def cls(self) -> list[str]:
        head = self.byte()
        if head == NIL:
            return []
        if head != NEW:
            return self.objects[head - REF_BASE]  # type: ignore[return-value]
        name = self.shared().decode()  # type: ignore[union-attr]
        self.int()  # version
        index = len(self.objects)
        self.objects.append(None)
        chain = [name] + self.cls()
        self.objects[index] = chain
        return chain

    def obj(self) -> object:
        head = self.byte()
        if head == NIL:
            return None
        if head != NEW:
            return self.objects[head - REF_BASE]
        index = len(self.objects)
        self.objects.append(None)
        chain = self.cls()
        name = chain[0]
        if name == "NSString" or name == "NSMutableString":
            assert self.shared() == b"+"
            value: object = self.raw(self.int()).decode("utf-8")
        elif name == "NSNumber":
            assert self.shared() == b"*"
            assert self.byte() == NEW
            ctype = self.shared()
            assert self.shared() == ctype
            value = Int32(self.int()) if ctype == b"i" else self.int()
        elif name in ("NSDictionary", "NSMutableDictionary"):
            assert self.shared() == b"i"
            count = self.int()
            d = {}
            for _ in range(count):
                assert self.shared() == b"@"
                k = self.obj()
                assert self.shared() == b"@"
                d[k] = self.obj()
            value = d
        elif name in ("NSAttributedString", "NSMutableAttributedString"):
            assert self.shared() == b"@"
            text = self.obj()
            runs = []
            while self.data[self.pos] != END:
                assert self.shared() == b"iI"
                self.int()  # ordinal
                length = self.int()
                assert self.shared() == b"@"
                runs.append((length, self.obj()))
            value = (text, runs)
        else:
            raise ValueError(f"unsupported class {name}")
        assert self.byte() == END, f"{name}: expected end of object"
        self.objects[index] = value
        return value


def read_attributed_string(data: bytes) -> tuple[str, list[tuple[int, dict]]]:
    """Decode an archived NSAttributedString into (text, runs)."""
    r = _Reader(data)
    if r.raw(13) != b"\x04\x0bstreamtyped":
        raise ValueError("not a typedstream")
    if r.int() != SYSTEM_VERSION:
        raise ValueError("unexpected system version")
    if r.shared() != b"@":
        raise ValueError("root is not an object")
    value = r.obj()
    if not (isinstance(value, tuple) and isinstance(value[0], str)):
        raise ValueError("root is not an NSAttributedString")
    return value  # type: ignore[return-value]


class UnsupportedArchive(ValueError):
    """The archive isn't an NSAttributedString this reader can get text from."""


@dataclass
class Body:
    text: str
    runs: list[tuple[int, dict]]
    complete: bool  # False when an attribute used a class the reader doesn't parse

    def attribute_values(self, key: str) -> list:
        return [a[key] for _, a in self.runs if key in a]


def decode_body(data: bytes) -> Body:
    """Text and (as far as possible) attribute runs from an attributedBody blob.

    Raises UnsupportedArchive when there is no text to recover. Never raises
    for attributes: runs stop at the first one it can't parse."""
    r = _Reader(data)
    try:
        if r.raw(13) != b"\x04\x0bstreamtyped" or r.int() != SYSTEM_VERSION or r.shared() != b"@":
            raise UnsupportedArchive("not a typedstream object archive")
        if r.byte() != NEW:
            raise UnsupportedArchive("root object missing")
        r.objects.append(None)
        chain = r.cls()
        if not chain or chain[0] not in ("NSAttributedString", "NSMutableAttributedString"):
            raise UnsupportedArchive(f"root is {chain[0] if chain else 'nil'}, not an attributed string")
        if r.shared() != b"@":
            raise UnsupportedArchive("attributed string has no text object")
        text = r.obj()
    except UnsupportedArchive:
        raise
    except (IndexError, AssertionError, ValueError, UnicodeDecodeError, struct.error, AttributeError) as exc:
        raise UnsupportedArchive(f"malformed archive: {exc}") from None
    if not isinstance(text, str):
        raise UnsupportedArchive("attributed string text is not a string")
    runs: list[tuple[int, dict]] = []
    try:
        while r.data[r.pos] != END:
            if r.shared() != b"iI":
                return Body(text, runs, False)
            r.int()
            length = r.int()
            if r.shared() != b"@":
                return Body(text, runs, False)
            attrs = r.obj()
            if not isinstance(attrs, dict):
                return Body(text, runs, False)
            runs.append((length, attrs))
    except (IndexError, AssertionError, ValueError, UnicodeDecodeError, struct.error, TypeError, AttributeError):
        return Body(text, runs, False)
    return Body(text, runs, True)
