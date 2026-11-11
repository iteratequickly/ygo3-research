"""YGO3 private server: Yu-Gi-Oh! Online 3 gate, front and duel servers plus a small HTTP server.

Layout of this file, top to bottom:
  1. configuration and protocol constants (ports, message and location ids)
  2. ocgcore message parsing and response builders (the duel engine's wire format)
  3. the native duel engine wrapper, the duel manager and the matchmaking router
  4. persistent state (accounts, teams, rentals, events), game data and the shop
  5. teams, trade/rental scenes, manner ratings and user events (module-level logic)
  6. the client wire format (Blowfish frames, field encoding)
  7. ClientSession (front/gate requests) and DuelSession (the duel port)
  8. server start-up and the command-line tools
Request ids are the client's (see protocol.md); replies and pushes use the ids named in comments.
"""
import asyncio
import logging
import os
import struct
import mimetypes
import pickle
import time
import sys
import zlib
import socket
import json
import ctypes
import re
import secrets
import sqlite3
import threading

from datetime import datetime, timedelta, timezone
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, unquote
from Crypto.Cipher import Blowfish

from typing import Optional

# Logger for everything in this file.
log = logging.getLogger("yo3.duel_engine")

# --- Paths and launcher configuration -------------------------------------------
APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "ygo3_launcher_config.json"


def load_server_config():
    """Loads settings from the launcher config file."""
    if CONFIG_FILE.is_file():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to parse config: {e}")
    return {}


config = load_server_config()

PROJECT_DIR = APP_DIR

DEFAULT_DLL = Path(config.get("ocg_dll", APP_DIR / "ocgcore.dll"))
DEFAULT_DATABASE = Path(config.get("card_database", APP_DIR / "cards.cdb"))
DEFAULT_SCRIPTS = Path(config.get("script_dir", APP_DIR / "script"))

# ocgcore's process() status codes.
OCG_DUEL_STATUS_END = 0
OCG_DUEL_STATUS_AWAITING = 1
OCG_DUEL_STATUS_CONTINUE = 2

TYPE_LINK = 0x4000000

DEFAULT_SCRIPT_FOLDERS = (
    "official",
    "pre-release",
    "pre-errata",
    "goat",
    "rush",
    "skill",
    "unofficial",
)

# ocgcore message type ids (MSG_*); MSG_NAME is the reverse lookup for logging.
MSG = dict(
    RETRY=1,
    HINT=2,
    WAITING=3,
    START=4,
    WIN=5,
    UPDATE_DATA=6,
    UPDATE_CARD=7,
    REQUEST_DECK=8,
    SELECT_BATTLECMD=10,
    SELECT_IDLECMD=11,
    SELECT_EFFECTYN=12,
    SELECT_YESNO=13,
    SELECT_OPTION=14,
    SELECT_CARD=15,
    SELECT_CHAIN=16,
    SELECT_PLACE=18,
    SELECT_POSITION=19,
    SELECT_TRIBUTE=20,
    SORT_CHAIN=21,
    SELECT_COUNTER=22,
    SELECT_SUM=23,
    SELECT_DISFIELD=24,
    SORT_CARD=25,
    SELECT_UNSELECT_CARD=26,
    CONFIRM_DECKTOP=30,
    CONFIRM_CARDS=31,
    SHUFFLE_DECK=32,
    SHUFFLE_HAND=33,
    REFRESH_DECK=34,
    SWAP_GRAVE_DECK=35,
    SHUFFLE_SET_CARD=36,
    REVERSE_DECK=37,
    DECK_TOP=38,
    SHUFFLE_EXTRA=39,
    NEW_TURN=40,
    NEW_PHASE=41,
    CONFIRM_EXTRATOP=42,
    MOVE=50,
    POS_CHANGE=53,
    SET=54,
    SWAP=55,
    FIELD_DISABLED=56,
    SUMMONING=60,
    SUMMONED=61,
    SPSUMMONING=62,
    SPSUMMONED=63,
    FLIPSUMMONING=64,
    FLIPSUMMONED=65,
    CHAINING=70,
    CHAINED=71,
    CHAIN_SOLVING=72,
    CHAIN_SOLVED=73,
    CHAIN_END=74,
    CHAIN_NEGATED=75,
    CHAIN_DISABLED=76,
    CARD_SELECTED=80,
    RANDOM_SELECTED=81,
    BECOME_TARGET=83,
    DRAW=90,
    DAMAGE=91,
    RECOVER=92,
    EQUIP=93,
    LPUPDATE=94,
    CARD_TARGET=96,
    CANCEL_TARGET=97,
    PAY_LPCOST=100,
    ADD_COUNTER=101,
    REMOVE_COUNTER=102,
    ATTACK=110,
    BATTLE=111,
    ATTACK_DISABLED=112,
    DAMAGE_STEP_START=113,
    DAMAGE_STEP_END=114,
    MISSED_EFFECT=120,
    BE_CHAIN_TARGET=121,
    CREATE_RELATION=122,
    RELEASE_RELATION=123,
    TOSS_COIN=130,
    TOSS_DICE=131,
    ROCK_PAPER_SCISSORS=132,
    HAND_RES=133,
    ANNOUNCE_RACE=140,
    ANNOUNCE_ATTRIB=141,
    ANNOUNCE_CARD=142,
    ANNOUNCE_NUMBER=143,
    CARD_HINT=160,
    TAG_SWAP=161,
    RELOAD_FIELD=162,
    AI_NAME=163,
    SHOW_HINT=164,
    PLAYER_HINT=165,
    MATCH_KILL=170,
    CUSTOM_MSG=180,
    REMOVE_CARDS=190,
)
MSG_NAME = {v: k for k, v in MSG.items()}

# Card locations, positions and query flags as ocgcore defines them.
LOC_DECK, LOC_HAND, LOC_MZONE, LOC_SZONE = 0x01, 0x02, 0x04, 0x08
LOC_GRAVE, LOC_REMOVED, LOC_EXTRA = 0x10, 0x20, 0x40
LOC_OVERLAY = 0x80

POS_FACEUP_ATTACK, POS_FACEDOWN_ATTACK = 0x1, 0x2
POS_FACEUP_DEFENSE, POS_FACEDOWN_DEFENSE = 0x4, 0x8

Q_CODE, Q_POSITION, Q_ALIAS, Q_TYPE = 0x1, 0x2, 0x4, 0x8
Q_LEVEL, Q_RANK, Q_ATTRIBUTE, Q_RACE = 0x10, 0x20, 0x40, 0x80
Q_ATTACK, Q_DEFENSE, Q_BASE_ATTACK, Q_BASE_DEFENSE = 0x100, 0x200, 0x400, 0x800
Q_REASON = 0x1000

R_BATTLECMD, R_IDLECMD, R_EFFECTYN, R_YESNO, R_OPTION = range(5)


# Raised when an ocgcore message buffer is truncated or malformed.
class ParseError(ValueError):
    pass


# Little-endian cursor over an ocgcore message buffer (u8/u16/u32/i32/u64/raw reads).
class Reader:
    __slots__ = ("b", "o")

    # Wraps `data`, starting at `offset`.
    def __init__(self, data: bytes, offset: int = 0):
        self.b = data
        self.o = offset

    # Bytes left to read.
    @property
    def avail(self) -> int:
        return len(self.b) - self.o

    # Reads an n-byte little-endian integer, raising ParseError past the end.
    def _take(self, n: int) -> int:
        if self.avail < n:
            raise ParseError("eof")
        start = self.o
        self.o += n
        return start

    # Reads one unsigned byte.
    def u8(self):
        return self.b[self._take(1)]

    # Reads an unsigned 16-bit integer.
    def u16(self):
        s = self._take(2)
        return struct.unpack_from("<H", self.b, s)[0]

    # Reads an unsigned 32-bit integer.
    def u32(self):
        s = self._take(4)
        return struct.unpack_from("<I", self.b, s)[0]

    # Reads a signed 32-bit integer.
    def i32(self):
        s = self._take(4)
        return struct.unpack_from("<i", self.b, s)[0]

    # Reads an unsigned 64-bit integer.
    def u64(self):
        s = self._take(8)
        return struct.unpack_from("<Q", self.b, s)[0]

    # Reads n raw bytes.
    def raw(self, n: int) -> bytes:
        s = self._take(n)
        return self.b[s : s + n]


# Parses a card location (controller, location, sequence, position).
def _loc(r: Reader) -> dict:
    controller = r.u8()
    location = r.u8()
    sequence = r.u32()
    position = r.u32()
    if location & LOC_OVERLAY:
        return {
            "controller": controller,
            "location": location & ~LOC_OVERLAY,
            "sequence": sequence,
            "position": POS_FACEUP_ATTACK,
            "overlay_sequence": position,
        }
    return {
        "controller": controller,
        "location": location,
        "sequence": sequence,
        "position": position,
    }


# True when a location is the engine's 'nowhere' placeholder.
def _loc_empty(loc: dict) -> bool:
    return not (
        loc["controller"]
        or loc["location"]
        or loc["sequence"]
        or loc["position"]
        or loc.get("overlay_sequence")
    )


# Reads `count` records with `fn`.
def _many(r: Reader, count, fn):
    return [fn() for _ in range(count)]


# Parses a card reference without location (code, controller...).
def _plain_card(r: Reader) -> dict:
    return {
        "code": r.u32(),
        "controller": r.u8(),
        "location": r.u8(),
        "sequence": r.u32(),
    }


# Parses a card code followed by its location.
def _card_loc(r: Reader) -> dict:
    d = {"code": r.u32()}
    d.update(_loc(r))
    return d


# Parses a full MSG_RELOAD_FIELD / field snapshot into a dict.
def _read_field(r: Reader) -> dict:
    flags = r.u32()
    players = []
    for _ in range(2):
        p = {"lp": r.u32()}
        monsters = []
        for _ in range(7):
            monsters.append(
                {"position": r.u8(), "materials": r.u32()} if r.u8() else None
            )
        spells = []
        for _ in range(8):
            spells.append(
                {"position": r.u8(), "materials": r.u32()} if r.u8() else None
            )
        p["monsters"], p["spells"] = monsters, spells
        p["deck_size"], p["hand_size"] = r.u32(), r.u32()
        p["grave_size"], p["banish_size"] = r.u32(), r.u32()
        p["extra_size"], p["extra_faceup_count"] = r.u32(), r.u32()
        players.append(p)
    chain = []
    for _ in range(r.u32()):
        chain.append(
            {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u32(),
                "position": r.u32(),
                "triggering_controller": r.u8(),
                "triggering_location": r.u8(),
                "triggering_sequence": r.u32(),
                "description": r.u64(),
            }
        )
    return {"flags": flags, "players": players, "chain": chain}


# Decodes ONE ocgcore message (by MSG_* type byte) into a dict; the big switch over every message
# layout.
def read_message(r: Reader):
    t = r.u8()
    m: dict = {"type": t}

    if t == MSG["RETRY"]:
        return m
    if t == MSG["HINT"]:
        m["hint_type"] = r.u8()
        m["player"] = r.u8()
        m["hint"] = r.u64() if r.avail > 4 else r.u32()
        return m
    if t == MSG["WIN"]:
        m["player"], m["reason"] = r.u8(), r.u8()
        return m
    if t == MSG["SELECT_BATTLECMD"]:
        m["player"] = r.u8()
        m["chains"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u32(),
                "description": r.u64(),
                "client_mode": r.u8(),
            },
        )
        m["attacks"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u8(),
                "can_direct": r.u8() != 0,
            },
        )
        m["to_m2"], m["to_ep"] = r.u8() != 0, r.u8() != 0
        return m
    if t == MSG["SELECT_IDLECMD"]:
        m["player"] = r.u8()
        m["summons"] = _many(r, r.u32(), lambda: _plain_card(r))
        m["special_summons"] = _many(r, r.u32(), lambda: _plain_card(r))
        m["pos_changes"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u8(),
            },
        )
        m["monster_sets"] = _many(r, r.u32(), lambda: _plain_card(r))
        m["spell_sets"] = _many(r, r.u32(), lambda: _plain_card(r))
        m["activates"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u32(),
                "description": r.u64(),
                "client_mode": r.u8(),
            },
        )
        m["to_bp"], m["to_ep"], m["shuffle"] = (r.u8() != 0, r.u8() != 0, r.u8() != 0)
        return m
    if t == MSG["SELECT_EFFECTYN"]:
        m["player"], m["code"] = r.u8(), r.u32()
        m.update(_loc(r))
        m["description"] = r.u64()
        return m
    if t == MSG["SELECT_YESNO"]:
        m["player"], m["description"] = r.u8(), r.u64()
        return m
    if t == MSG["SELECT_OPTION"]:
        m["player"] = r.u8()
        m["options"] = _many(r, r.u8(), r.u64)
        return m
    if t == MSG["SELECT_CARD"]:
        m["player"], m["can_cancel"] = r.u8(), r.u8() != 0
        m["min"], m["max"] = r.u32(), r.u32()
        m["selects"] = _many(r, r.u32(), lambda: _card_loc(r))
        return m
    if t == MSG["SELECT_CHAIN"]:
        m["player"], m["spe_count"] = r.u8(), r.u8()
        m["forced"] = r.u8() != 0
        m["hint_timing"], m["hint_timing_other"] = r.u32(), r.u32()

        def chain_sel():
            d = _card_loc(r)
            d["description"] = r.u64()
            d["client_mode"] = r.u8()
            return d

        m["selects"] = _many(r, r.u32(), chain_sel)
        return m
    if t in (MSG["SELECT_PLACE"], MSG["SELECT_DISFIELD"]):
        m["player"], m["count"], m["field_mask"] = r.u8(), r.u8(), r.u32()
        return m
    if t == MSG["SELECT_POSITION"]:
        m["player"], m["code"], m["positions"] = r.u8(), r.u32(), r.u8()
        return m
    if t == MSG["SELECT_TRIBUTE"]:
        m["player"], m["can_cancel"] = r.u8(), r.u8() != 0
        m["min"], m["max"] = r.u32(), r.u32()
        m["selects"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u32(),
                "release_param": r.u8(),
            },
        )
        return m
    if t in (MSG["SORT_CHAIN"], MSG["SORT_CARD"]):
        m["player"] = r.u8()
        m["cards"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u32(),
                "sequence": r.u32(),
            },
        )
        return m
    if t == MSG["SELECT_COUNTER"]:
        m["player"], m["counter_type"], m["count"] = r.u8(), r.u16(), r.u16()
        m["cards"] = _many(
            r,
            r.u32(),
            lambda: {
                "code": r.u32(),
                "controller": r.u8(),
                "location": r.u8(),
                "sequence": r.u8(),
                "count": r.u16(),
            },
        )
        return m
    if t == MSG["SELECT_SUM"]:
        m["player"], m["select_max"] = r.u8(), r.u8()
        m["amount"], m["min"], m["max"] = r.u32(), r.u32(), r.u32()

        def sum_sel():
            d = _card_loc(r)
            d["amount"] = r.u32()
            return d

        m["selects_must"] = _many(r, r.u32(), sum_sel)
        m["selects"] = _many(r, r.u32(), sum_sel)
        return m
    if t == MSG["SELECT_UNSELECT_CARD"]:
        m["player"] = r.u8()
        m["can_finish"], m["can_cancel"] = r.u8() != 0, r.u8() != 0
        m["min"], m["max"] = r.u32(), r.u32()
        m["select_cards"] = _many(r, r.u32(), lambda: _card_loc(r))
        m["unselect_cards"] = _many(r, r.u32(), lambda: _card_loc(r))
        return m
    if t in (MSG["CONFIRM_DECKTOP"], MSG["CONFIRM_CARDS"], MSG["CONFIRM_EXTRATOP"]):
        m["player"] = r.u8()
        m["cards"] = _many(r, r.u32(), lambda: _plain_card(r))
        return m
    if t == MSG["SHUFFLE_DECK"]:
        m["player"] = r.u8()
        return m
    if t in (MSG["SHUFFLE_HAND"], MSG["SHUFFLE_EXTRA"]):
        m["player"] = r.u8()
        m["cards"] = _many(r, r.u32(), r.u32)
        return m
    if t in (MSG["REFRESH_DECK"], MSG["REVERSE_DECK"]):
        return m
    if t == MSG["SWAP_GRAVE_DECK"]:
        m["player"] = r.u8()
        size = r.u32()
        m["deck_size"] = size
        data = r.raw(r.u32())
        m["returned_to_extra"] = [
            i for i in range(size) if data[i // 8] & (1 << (i % 8))
        ]
        return m
    if t == MSG["SHUFFLE_SET_CARD"]:
        m["location"] = r.u8()
        m["cards"] = _many(r, r.u8(), lambda: {"from": _loc(r), "to": _loc(r)})
        return m
    if t == MSG["DECK_TOP"]:
        m["player"], m["count"] = r.u8(), r.u32()
        m["code"], m["position"] = r.u32(), r.u32()
        return m
    if t == MSG["NEW_TURN"]:
        m["player"] = r.u8()
        return m
    if t == MSG["NEW_PHASE"]:
        m["phase"] = r.u16()
        return m
    if t == MSG["MOVE"]:
        m["card"] = r.u32()
        m["from"], m["to"] = _loc(r), _loc(r)
        m["reason"] = r.u32() if r.avail >= 4 else 0
        return m
    if t == MSG["POS_CHANGE"]:
        m["code"], m["controller"], m["location"] = r.u32(), r.u8(), r.u8()
        m["sequence"], m["prev_position"], m["position"] = (r.u8(), r.u8(), r.u8())
        return m
    if t in (MSG["SET"], MSG["SUMMONING"], MSG["SPSUMMONING"], MSG["FLIPSUMMONING"]):
        m["code"] = r.u32()
        m.update(_loc(r))
        return m
    if t == MSG["SWAP"]:
        m["card1"], m["card2"] = _card_loc(r), _card_loc(r)
        return m
    if t == MSG["FIELD_DISABLED"]:
        m["field_mask"] = r.u32()
        return m
    if t in (
        MSG["SUMMONED"],
        MSG["SPSUMMONED"],
        MSG["FLIPSUMMONED"],
        MSG["CHAIN_END"],
        MSG["ATTACK_DISABLED"],
        MSG["DAMAGE_STEP_START"],
        MSG["DAMAGE_STEP_END"],
        MSG["BE_CHAIN_TARGET"],
        MSG["CREATE_RELATION"],
        MSG["RELEASE_RELATION"],
        MSG["CUSTOM_MSG"],
    ):
        return m
    if t == MSG["CHAINING"]:
        m["code"] = r.u32()
        m.update(_loc(r))
        m["triggering_controller"], m["triggering_location"] = r.u8(), r.u8()
        m["triggering_sequence"] = r.u32()
        m["description"], m["chain_size"] = r.u64(), r.u32()
        return m
    if t in (
        MSG["CHAINED"],
        MSG["CHAIN_SOLVING"],
        MSG["CHAIN_SOLVED"],
        MSG["CHAIN_NEGATED"],
        MSG["CHAIN_DISABLED"],
    ):
        m["chain_size"] = r.u8()
        return m
    if t in (MSG["CARD_SELECTED"], MSG["BECOME_TARGET"], MSG["REMOVE_CARDS"]):
        m["cards"] = _many(r, r.u32(), lambda: _loc(r))
        return m
    if t == MSG["RANDOM_SELECTED"]:
        m["player"] = r.u8()
        m["cards"] = _many(r, r.u32(), lambda: _loc(r))
        return m
    if t == MSG["DRAW"]:
        m["player"] = r.u8()
        m["drawn"] = _many(r, r.u32(), lambda: {"code": r.u32(), "position": r.u32()})
        return m
    if t in (MSG["DAMAGE"], MSG["RECOVER"], MSG["PAY_LPCOST"]):
        m["player"], m["amount"] = r.u8(), r.u32()
        return m
    if t == MSG["LPUPDATE"]:
        m["player"], m["lp"] = r.u8(), r.u32()
        return m
    if t in (MSG["EQUIP"], MSG["CARD_TARGET"], MSG["CANCEL_TARGET"]):
        m["card"], m["target"] = _loc(r), _loc(r)
        return m
    if t in (MSG["ADD_COUNTER"], MSG["REMOVE_COUNTER"]):
        m["counter_type"], m["controller"] = r.u16(), r.u8()
        m["location"], m["sequence"], m["count"] = r.u8(), r.u8(), r.u16()
        return m
    if t == MSG["ATTACK"]:
        m["card"] = _loc(r)
        target = _loc(r)
        m["target"] = None if _loc_empty(target) else target
        return m
    if t == MSG["BATTLE"]:
        for key in ("card", "target"):
            d = _loc(r)
            d["attack"], d["defense"] = r.u32(), r.u32()
            d["destroyed"] = r.u8() != 0
            m[key] = d
        return m
    if t == MSG["MISSED_EFFECT"]:
        m.update(_loc(r))
        m["code"] = r.u32()
        return m
    if t == MSG["TOSS_COIN"]:
        m["player"] = r.u8()
        m["results"] = _many(r, r.u8(), lambda: r.u8() != 0)
        return m
    if t == MSG["TOSS_DICE"]:
        m["player"] = r.u8()
        m["results"] = _many(r, r.u8(), r.u8)
        return m
    if t == MSG["ROCK_PAPER_SCISSORS"]:
        m["player"] = r.u8()
        return m
    if t == MSG["HAND_RES"]:
        v = r.u8()
        m["results"] = (v & 3, (v >> 2) & 3)
        return m
    if t == MSG["ANNOUNCE_RACE"]:
        m["player"], m["count"], m["available"] = r.u8(), r.u8(), r.u64()
        return m
    if t == MSG["ANNOUNCE_ATTRIB"]:
        m["player"], m["count"], m["available"] = r.u8(), r.u8(), r.u32()
        return m
    if t in (MSG["ANNOUNCE_CARD"], MSG["ANNOUNCE_NUMBER"]):
        m["player"] = r.u8()
        key = "opcodes" if t == MSG["ANNOUNCE_CARD"] else "options"
        m[key] = _many(r, r.u8(), r.u64)
        return m
    if t == MSG["CARD_HINT"]:
        m.update(_loc(r))
        m["card_hint"], m["description"] = r.u8(), r.u64()
        return m
    if t == MSG["TAG_SWAP"]:
        m["player"] = r.u8()
        deck, extra = r.u32(), r.u32()
        m["deck_size"], m["extra_faceup_count"] = deck, r.u32()
        hand = r.u32()
        top = r.u32()
        m["deck_top_card"] = None if top == 0 else top
        m["hand"] = _many(r, hand, lambda: {"code": r.u32(), "position": r.u32()})
        m["extra"] = _many(r, extra, lambda: {"code": r.u32(), "position": r.u32()})
        return m
    if t == MSG["RELOAD_FIELD"]:
        m.update(_read_field(r))
        return m
    if t in (MSG["AI_NAME"], MSG["SHOW_HINT"]):
        key = "name" if t == MSG["AI_NAME"] else "hint"
        m[key] = r.raw(r.u16()).decode("utf-8", "replace")
        return m
    if t == MSG["PLAYER_HINT"]:
        m["player"], m["player_hint"], m["description"] = (r.u8(), r.u8(), r.u64())
        return m
    if t == MSG["MATCH_KILL"]:
        m["card"] = r.u32()
        return m
    return None


# Splits an engine output buffer into messages; `strict` raises on a bad one, else stops quietly.
def parse_messages(buf: bytes, strict: bool = True) -> list[dict]:
    out = []
    r = Reader(buf)
    while r.avail > 0:
        length = r.i32()
        if length < 1 or length > r.avail:
            raise ParseError(f"bad frame length {length} ({r.avail} left)")
        body = r.raw(length)
        sub = Reader(body)
        try:
            msg = read_message(sub)
        except ParseError:
            if strict:
                raise
            msg = None
        if msg is None:
            if strict and body[0] not in MSG_NAME:
                raise ParseError(f"unknown message type {body[0]}")
            out.append({"type": body[0], "unparsed": body})
            continue
        if strict and sub.avail:
            raise ParseError(
                f"{MSG_NAME.get(msg['type'], msg['type'])}: "
                f"{sub.avail} unread bytes"
            )
        out.append(msg)
    return out


# Interprets a value as a signed 32-bit integer.
def _i32(v):
    return struct.pack("<i", v)


# Builds the response to an idle/battle command prompt: action in the high bits, card index in the
# low.
def resp_idle(action: int, index: int = 0) -> bytes:
    return _i32(action | (index << 16))


resp_battle = resp_idle


# Response to a yes/no prompt.
def resp_bool(yes: bool) -> bytes:
    return _i32(1 if yes else 0)


# Response choosing one option by index.
def resp_index(i: int) -> bytes:
    return _i32(i)


# Response selecting several cards by index (count then indices).
def resp_cards(indices) -> bytes:
    if indices is None:
        return _i32(-1)
    return _i32(0) + _i32(len(indices)) + b"".join(_i32(i) for i in indices)


# Response to a select/unselect prompt; None/-1 finishes the selection.
def resp_unselect(index) -> bytes:
    if index is None:
        return _i32(-1)
    return _i32(1) + _i32(index)


# Response to a chain prompt; -1 declines to chain.
def resp_chain(index) -> bytes:
    return _i32(-1 if index is None else index)


# Response choosing field places (controller, location, sequence triples).
def resp_places(places) -> bytes:
    return b"".join(struct.pack("<bbb", p, l, s) for p, l, s in places)


# Response choosing a card position.
def resp_position(position: int) -> bytes:
    return _i32(position)


# Response distributing counters over cards.
def resp_counters(counts) -> bytes:
    return b"".join(struct.pack("<h", c) for c in counts)


# Response ordering cards (chain/card sort prompts); empty keeps the default order.
def resp_sort(order) -> bytes:
    if order is None:
        return struct.pack("<b", -1)
    return struct.pack("<b", len(order)) + bytes(order)


# Response announcing a monster-race mask.
def resp_race(mask: int) -> bytes:
    return struct.pack("<Q", mask)


# Response announcing an attribute mask.
def resp_attrib(mask: int) -> bytes:
    return struct.pack("<I", mask)


# Response announcing a number.
def resp_number(value: int) -> bytes:
    return _i32(value)


resp_card_code = resp_number
resp_rps = resp_number


# ctypes mirror of the engine's query request structure.
class OCG_QueryInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("con", ctypes.c_uint8),
        ("loc", ctypes.c_uint32),
        ("seq", ctypes.c_uint32),
        ("overlay_seq", ctypes.c_uint32),
    ]


# Declares argument/return types of the engine's card-query functions.
def bind_queries(dll) -> None:
    if getattr(dll, "_yo3_query_bound", False):
        return
    for name in ("OCG_DuelQuery", "OCG_DuelQueryLocation"):
        fn = getattr(dll, name)
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(OCG_QueryInfo),
        ]
        fn.restype = ctypes.c_void_p
    dll.OCG_DuelQueryCount.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32]
    dll.OCG_DuelQueryCount.restype = ctypes.c_uint32
    dll._yo3_query_bound = True


# Decodes the byte blob the engine returns for a card query (flag-driven fields).
def _read_query(r: Reader):
    out = {}
    while r.avail > 0:
        size = r.u16()
        if size == 0:
            return None
        if size < 4:
            break
        flag = r.u32()
        size -= 4
        if flag == 0x80000000:
            break
        body = r.raw(size)
        if (
            flag
            in (
                Q_CODE,
                Q_POSITION,
                Q_ALIAS,
                Q_TYPE,
                Q_LEVEL,
                Q_RANK,
                Q_ATTRIBUTE,
                Q_ATTACK,
                Q_DEFENSE,
                Q_BASE_ATTACK,
                Q_BASE_DEFENSE,
                Q_REASON,
            )
            and size == 4
        ):
            name = {
                Q_CODE: "code",
                Q_POSITION: "position",
                Q_ALIAS: "alias",
                Q_TYPE: "type",
                Q_LEVEL: "level",
                Q_RANK: "rank",
                Q_ATTRIBUTE: "attribute",
                Q_ATTACK: "attack",
                Q_DEFENSE: "defense",
                Q_BASE_ATTACK: "base_attack",
                Q_BASE_DEFENSE: "base_defense",
                Q_REASON: "reason",
            }[flag]
            out[name] = struct.unpack("<I", body)[0]
        elif flag == Q_RACE and size == 8:
            out["race"] = struct.unpack("<Q", body)[0]
    return out


# Queries one card's fields (code, position, level, ATK...) from the live duel.
def query_card(
    dll,
    handle,
    flags: int,
    controller: int,
    location: int,
    sequence: int,
    overlay: int = 0,
):
    bind_queries(dll)
    info = OCG_QueryInfo(flags, controller, location, sequence, overlay)
    length = ctypes.c_uint32(0)
    ptr = dll.OCG_DuelQuery(handle, ctypes.byref(length), ctypes.byref(info))
    if not ptr or not length.value:
        return None
    return _read_query(Reader(ctypes.string_at(ptr, length.value)))


# Queries every card in one location (e.g. a player's monster zones).
def query_location(dll, handle, flags: int, controller: int, location: int):
    bind_queries(dll)
    info = OCG_QueryInfo(flags, controller, location, 0, 0)
    length = ctypes.c_uint32(0)
    ptr = dll.OCG_DuelQueryLocation(handle, ctypes.byref(length), ctypes.byref(info))
    if not ptr or not length.value:
        return []
    r = Reader(ctypes.string_at(ptr, length.value))
    r.u32()
    cards = []
    while r.avail > 0:
        cards.append(_read_query(r))
    return cards


MONSTER_STATS = (
    Q_POSITION | Q_LEVEL | Q_ATTACK | Q_DEFENSE | Q_BASE_ATTACK | Q_BASE_DEFENSE
)


# Both players' monsters on the field with their stats.
def field_monsters(dll, handle) -> list[dict]:
    out = []
    for controller in (0, 1):
        for seq, card in enumerate(
            query_location(dll, handle, MONSTER_STATS, controller, LOC_MZONE)
        ):
            if not card:
                continue
            out.append({"controller": controller, "sequence": seq, **card})
    return out


# Fetches the engine's 'reason' flags for a card that just moved.
def move_reason(dll, handle, move: dict) -> int:
    to = move["to"]
    if not to["location"] or (to["location"] & LOC_OVERLAY):
        return 0
    card = query_card(
        dll, handle, Q_REASON, to["controller"], to["location"], to["sequence"]
    )
    return int((card or {}).get("reason", 0))


PROMPTS = frozenset(
    MSG[n]
    for n in (
        "SELECT_BATTLECMD",
        "SELECT_IDLECMD",
        "SELECT_EFFECTYN",
        "SELECT_YESNO",
        "SELECT_OPTION",
        "SELECT_CARD",
        "SELECT_CHAIN",
        "SELECT_PLACE",
        "SELECT_POSITION",
        "SELECT_TRIBUTE",
        "SORT_CHAIN",
        "SELECT_COUNTER",
        "SELECT_SUM",
        "SELECT_DISFIELD",
        "SORT_CARD",
        "SELECT_UNSELECT_CARD",
        "ROCK_PAPER_SCISSORS",
        "ANNOUNCE_RACE",
        "ANNOUNCE_ATTRIB",
        "ANNOUNCE_CARD",
        "ANNOUNCE_NUMBER",
    )
)

STATUS_END, STATUS_AWAITING, STATUS_CONTINUE = 0, 1, 2


# Result of one engine step: parsed events, the prompt (if any) and end/winner info.
class Step:
    __slots__ = ("events", "prompt", "ended", "retry", "winner", "reason")

    # Starts an empty step.
    def __init__(self):
        self.events: list[dict] = []
        self.prompt: dict | None = None
        self.ended = False
        self.retry = False
        self.winner = None
        self.reason = None

    # Compact debug text listing event names and the prompt.
    def __repr__(self):
        names = [MSG_NAME.get(e["type"], e["type"]) for e in self.events]
        p = MSG_NAME.get(self.prompt["type"]) if self.prompt else None
        return f"<Step events={names} prompt={p} ended={self.ended}>"


# Drives a NativeDuel step by step, turning raw output into Step objects.
class DuelDriver:
    # Binds the driver to a native duel.
    def __init__(self, native):
        self.native = native
        self.dll = native.engine.dll
        bind_queries(self.dll)
        self.finished = False
        self.last_prompt: dict | None = None

    # The underlying native duel handle.
    @property
    def handle(self):
        return self.native.handle

    # Parses the engine's raw output into a Step, noting prompts and the end of the duel.
    def _collect(self, status: int, raw: bytes) -> Step:
        step = Step()
        for msg in parse_messages(raw):
            t = msg["type"]
            if t == MSG["RETRY"]:
                step.retry = True
            elif t in PROMPTS:
                step.prompt = msg
            else:
                step.events.append(msg)
                if t == MSG["WIN"]:
                    step.winner, step.reason = msg["player"], msg["reason"]
                    step.ended = True
        if status == STATUS_END:
            step.ended = True
        if step.ended:
            step.prompt = None
            self.finished = True
        elif step.prompt is not None:
            self.last_prompt = step.prompt
        return step

    # Advances the engine until it needs an answer or ends.
    def step(self) -> Step:
        status, raw = self.native.process_until_waiting()
        return self._collect(status, raw)

    # Feeds a response to the pending prompt and advances.
    def answer(self, response: bytes) -> Step:
        if self.finished:
            raise RuntimeError("the duel has ended")
        status, raw = self.native.respond_and_process(response)
        return self._collect(status, raw)

    # Monsters currently on the field.
    def monsters(self) -> list[dict]:
        return field_monsters(self.dll, self.handle)


# --- Network, protocol and server settings ---------------------------------------
# (HOST/ports, the shared Blowfish key, the login identity, event and duel announce options)
HOST = os.getenv("YO3_HOST", config["host"])
FRONT_ADDRESS = config["front_address"]
GATE_PORT = int(config["gate_port"])
FRONT_PORT = int(config["front_port"])
DUEL_PORT = int(config["duel_port"])
HTTP_PORT = int(os.getenv("YO3_HTTP_PORT", config["http_port"]))

KEY = b"12345678"
LENGTH_MASK = 0x77777777

MAX_PACKET_SIZE = 1024 * 1024
MAX_NESTING = 16

VERSION = b"\x00\x12" + b"2010.03.23 skelton"

USER_ID = 1001
USERNAME = "test"

REGULATION_ID = 3

EVENT_ID = 1
EVENT_TYPE = int(os.getenv("YO3_EVENT_TYPE", "1"))
EVENT_STATUS = int(os.getenv("YO3_EVENT_STATUS", "1"))
EVENT_LAYOUT = os.getenv("YO3_EVENT_LAYOUT", "items").lower()
EVENT_COUNTRY = "GB"

DUEL_ANNOUNCE = os.getenv("YO3_DUEL_ANNOUNCE", "both").lower()

AUX_SUCCESS_REPLY = {
    0x17F2: 0x17F3,
    0x17A2: 0x17A3,
    0x178E: 0x178F,
    0x2580: 0x2581,
    0x271A: 0x271B,
    0x2775: 0x2778,
    0x9500: 0x9501,
    0x9514: 0x9515,
    0x11628: 0x11629,
    0x10010: 0x10011,
    0x10101: 0x10104,
    0x9603: 0x9608,
}

DUEL_POINTS = 10000
DUEL_TURN_TIME = 180
DUEL_TOTAL_TIME = 600
PREMIUM = 1
# Duelist Premium (the eagle) uses a fixed date value (2036-11-11) that the client parses.
PREMIUM_EXPIRE = "2036-11-11 00:00:00"
# Bug: if you select the eagle the client crashes.

LANG_COUNTRY = {0: "JP", 1: "GB", 2: "DE", 3: "FR", 4: "IT", 5: "ES", 6: "KR", 7: "CN"}

LEGACY = {x.strip() for x in os.getenv("YO3_LEGACY", "").split(",") if x.strip()}

JST = timezone(timedelta(hours=9))

MAX_FRIENDS = 100
MAIL_TITLE_MAX = 16
# --- Social limits ------------------------------------------------------------------
MAIL_BODY_MAX = 255

# --- Teams ---------------------------------------------------------------
TEAM_NAME_MAX = 16  # characters in a team name
TEAM_LEADER, TEAM_SUBLEADER, TEAM_MEMBER = 2, 1, 0  # the authority values

# --- Trade and rental scenes ---------------------------------------------
TRADE_OFFER_MAX = 500  # most card rows one trade offer may hold (defensive cap)
RENTAL_DAYS = (1, 3, 7, 14)  # the terms the lender's dialog offers
RENTAL_FREE_MAX = 90  # a Free rental holds at most this many cards
RENTAL_DECK_MAIN_MAX = 60  # a Deck rental's main list
RENTAL_DECK_SIDE_MAX = 15  # a Deck rental's extra and side lists

# --- Manner evaluations ---------------------------------------------------
EVALUATION_VALUES = (-10, -5, 0, 5, 10)  # the five buttons of dlg_manner.swf
EVALUATIONS_KEPT = 1000  # unanswered evaluations kept before the oldest go

# --- User events (tournaments) ---------------------------------------------
USER_EVENT_OPEN, USER_EVENT_RUNNING, USER_EVENT_DONE = 12, 13, 14  # list statuses
USER_EVENT_MAX_ENTRY = 512  # 64 blocks of 8 seats
USER_EVENT_MAX_OPEN = 100  # events listed at once ("full" in create_status)
USER_EVENTS_FINISHED_KEPT = 50  # finished events a lobby can still ask about
ROUND_DELAY = float(os.getenv("YO3_ROUND_DELAY", "20"))  # seconds between rounds

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "yo3_state.pkl"


# Reads the game folder from --game-dir on the command line, if given.
def game_dir_from_args():
    if "--game-dir" in sys.argv:
        index = sys.argv.index("--game-dir")
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return None


# Finds the game folder: config, environment, the command line, else next to the server.
def _default_game_dir():
    if os.name == "nt":
        return r"C:\Program Files (x86)\Konami\Yu-Gi-Oh! ONLINE 3"
    return os.path.join(
        os.path.expanduser("~"),
        ".wine/drive_c/Program Files (x86)/Konami/Yu-Gi-Oh! ONLINE 3",
    )


GAME_DIR = Path(
    game_dir_from_args() or os.getenv("YO3_GAME_DIR") or _default_game_dir()
).expanduser()

WEB_ROOT = Path(__file__).resolve().parent / "www"
WEB_ROOT.mkdir(exist_ok=True)

# --- Limits and prices of the account, deck editor and shop ---------------------
MAX_PLAYERS = 4
MAX_DECKS = 10
MAX_DECK_ROWS = 100
MAX_DECK_NAME = 64

PACK_COST = 100
PACK_MAX = 10

DECK_COST = 500
DECK_MAX = 3
DECK_SALE = 20000

PROTECTOR_COST = 200
AVATAR_ITEM_COST = 100

ITEM_SALE = 30000

PROTECTOR_TYPE = 6
AVATAR_ITEM_TYPE = 3

ITEM_DATE = "2010-03-23 00:00:00"
COMMONS = 4

FALLBACK_CARD_ROWS = 4833

# --- Starter and official decks granted to new players ------------------------------
STARTER_REGULATION = 1
STARTER_DECK_ID = 1
STARTER_DECK_NAME = "Starter Deck"

GRANT_STARTER_DECK = True
GRANT_OFFICIAL_DECKS = True

OFFICIAL_DECKS = (
    (121, "Yugi"),
    (147, "Jaden"),
    (171, "Yusei"),
    (120, "Yugi"),
    (148, "Jaden"),
    (172, "Yusei"),
    (122, "Yugi"),
    (149, "Jaden"),
    (173, "Yusei"),
)

NPC_DECK_TITLE = 0x5FB4
NEVER_ALLOWED_CARDS = (5280, 5434, 6276)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("YO3Server")


# ctypes mirror: a player's starting LP, hand and draw count.
class OCG_Player(ctypes.Structure):
    _fields_ = [
        ("startingLP", ctypes.c_uint32),
        ("startingDrawCount", ctypes.c_uint32),
        ("drawCountPerTurn", ctypes.c_uint32),
    ]


# ctypes mirror: one card's database row as ocgcore wants it.
class OCG_CardData(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint32),
        ("alias", ctypes.c_uint32),
        ("setcodes", ctypes.POINTER(ctypes.c_uint16)),
        ("type", ctypes.c_uint32),
        ("level", ctypes.c_uint32),
        ("attribute", ctypes.c_uint32),
        ("race", ctypes.c_uint64),
        ("attack", ctypes.c_int32),
        ("defense", ctypes.c_int32),
        ("lscale", ctypes.c_uint32),
        ("rscale", ctypes.c_uint32),
        ("link_marker", ctypes.c_uint32),
    ]


# ctypes mirror: where to place a new card when building a duel.
class OCG_NewCardInfo(ctypes.Structure):
    _fields_ = [
        ("team", ctypes.c_uint8),
        ("duelist", ctypes.c_uint8),
        ("code", ctypes.c_uint32),
        ("con", ctypes.c_uint8),
        ("loc", ctypes.c_uint32),
        ("seq", ctypes.c_uint32),
        ("pos", ctypes.c_uint32),
    ]


OCG_DataReader = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(OCG_CardData)
)

OCG_DataReaderDone = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.POINTER(OCG_CardData)
)

OCG_ScriptReader = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
)

OCG_LogHandler = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)


# ctypes mirror: duel creation options (seed, flags, readers, players).
class OCG_DuelOptions(ctypes.Structure):
    _fields_ = [
        ("seed", ctypes.c_uint64 * 4),
        ("flags", ctypes.c_uint64),
        ("team1", OCG_Player),
        ("team2", OCG_Player),
        ("cardReader", OCG_DataReader),
        ("payload1", ctypes.c_void_p),
        ("scriptReader", OCG_ScriptReader),
        ("payload2", ctypes.c_void_p),
        ("logHandler", OCG_LogHandler),
        ("payload3", ctypes.c_void_p),
        ("cardReaderDone", OCG_DataReaderDone),
        ("payload4", ctypes.c_void_p),
        ("enableUnsafeLibraries", ctypes.c_uint8),
    ]


# Read-only access to the OCG cards.cdb (stats, texts, setcodes) and the YO3-row -> passcode
# mapping.
class CardDatabase:
    # Opens the database and loads its name index.
    def __init__(self, database_path: Path | str):
        self.path = Path(database_path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"Card database not found: {self.path}")

        self.connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._yo3_cid_passcodes = {}

        mapping_file = self.path.parent / "cardinfo.json"
        if mapping_file.is_file():
            try:
                import json

                payload = json.loads(mapping_file.read_text(encoding="utf-8"))
                for entry in payload.get("data", []):
                    passcode = int(entry.get("id", 0))
                    for misc in entry.get("misc_info", []) or []:
                        cid = int(misc.get("konami_id", 0) or 0)
                        if cid and passcode:
                            self._yo3_cid_passcodes.setdefault(cid, passcode)
                log.info(
                    "Loaded %d authoritative YO3 CID/passcode mappings",
                    len(self._yo3_cid_passcodes),
                )
            except Exception:
                log.exception(
                    "Could not load optional cardinfo.json; using CDB name/stat matching"
                )

        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(datas)")
        }
        required = {
            "id",
            "alias",
            "setcode",
            "type",
            "atk",
            "def",
            "level",
            "race",
            "attribute",
        }
        missing = required - columns

        if missing:
            self.connection.close()
            raise RuntimeError(
                "Unsupported cards.cdb schema. Missing: " + ", ".join(sorted(missing))
            )

        log.info("Loaded card database: %s", self.path)

    # Splits the packed 64-bit setcode column into its 16-bit archetype codes.
    @staticmethod
    def unpack_setcodes(value: int) -> list[int]:
        packed = int(value or 0) & 0xFFFFFFFFFFFFFFFF
        result = []
        for index in range(4):
            item = (packed >> (index * 16)) & 0xFFFF
            if item:
                result.append(item)
        return result

    # Returns the database row for a passcode, or None.
    def get_card(self, code: int):
        with self.lock:
            return self.connection.execute(
                "SELECT id, alias, setcode, type, atk, def, level, race, attribute FROM datas WHERE id = ?",
                (int(code),),
            ).fetchone()

    # Lower-cases and strips punctuation so names compare loosely.
    @staticmethod
    def _normalise_card_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())

    # Finds rows whose name matches a YO3 name shortened with '...'.
    def _abbreviated_name_rows(self, yo3_name: str):
        tokens = yo3_name.split()
        if not any(token.endswith(".") for token in tokens):
            return []

        def token_matches(yo3_token, cdb_token):
            wanted = self._normalise_card_name(yo3_token)
            have = self._normalise_card_name(cdb_token)
            if yo3_token.endswith("."):
                return bool(wanted) and have.startswith(wanted)
            return wanted == have

        with self.lock:
            candidates = self.connection.execute(
                "SELECT d.id, d.alias, d.atk, d.def, d.level, d.attribute, d.race, t.name FROM texts t JOIN datas d ON d.id=t.id"
            ).fetchall()

        matches = []
        for row in candidates:
            parts = str(row["name"] or "").split()
            if len(parts) == len(tokens) and all(
                token_matches(a, b) for a, b in zip(tokens, parts)
            ):
                matches.append(row)

        return matches

    # Maps a YO3 card (by name/cid) to the OCG passcode, trying exact, normalised and abbreviated
    # names.
    def resolve_yo3_card(self, card) -> int:
        cid = int(getattr(card, "cid", 0) or 0)
        mapped = self._yo3_cid_passcodes.get(cid)
        if mapped:
            if self.get_card(mapped) is None:
                raise DuelDeckError(
                    f"cardinfo.json maps YO3 CID {cid} to missing CDB passcode {mapped}"
                )
            return mapped

        name = self._normalise_card_name(getattr(card, "name", ""))
        if not name:
            raise DuelDeckError(
                f"YO3 CID {getattr(card, 'cid', '?')} has no name for passcode lookup"
            )

        with self.lock:
            rows = self.connection.execute(
                "SELECT d.id, d.alias, d.atk, d.def, d.level, d.attribute, d.race, t.name FROM texts t JOIN datas d ON d.id=t.id WHERE lower(t.name)=lower(?)",
                (str(card.name),),
            ).fetchall()
            if not rows:
                rows = [
                    r
                    for r in self.connection.execute(
                        "SELECT d.id, d.alias, d.atk, d.def, d.level, d.attribute, d.race, t.name FROM texts t JOIN datas d ON d.id=t.id"
                    )
                    if self._normalise_card_name(r["name"]) == name
                ]

        if not rows:
            rows = self._abbreviated_name_rows(str(card.name))

        if not rows:
            raise DuelDeckError(
                f"No cards.cdb passcode found for YO3 CID {card.cid} ({card.name!r})"
            )

        def score(row):
            level = int(row["level"] or 0) & 0xFF
            checks = (
                (int(row["atk"] or 0), int(getattr(card, "atk", -999))),
                (int(row["def"] or 0), int(getattr(card, "defense", -999))),
                (level, int(getattr(card, "level", -999))),
                (int(row["attribute"] or 0), int(getattr(card, "attribute", -999))),
                (int(row["race"] or 0), int(getattr(card, "race", -999))),
            )
            return sum(a == b for a, b in checks)

        ranked = sorted(((score(r), int(r["id"])) for r in rows), reverse=True)
        best = ranked[0][0]
        winners = sorted({code for points, code in ranked if points == best})

        if len(winners) > 1:
            aliases = {int(r["id"]): int(r["alias"] or 0) for r in rows}
            canonical = [c for c in winners if aliases.get(c, 0) not in winners]
            if len(canonical) == 1:
                winners = canonical
            elif canonical and best >= 2:
                log.warning(
                    "YO3 CID %s (%r) matches several identical CDB entries %s; using %d",
                    card.cid,
                    card.name,
                    canonical,
                    canonical[0],
                )
                winners = canonical[:1]

        if len(winners) != 1 or best < 2:
            raise DuelDeckError(
                f"Ambiguous passcode for YO3 CID {card.cid} ({card.name!r}); candidates={winners}, matched_stats={best}/5."
            )
        return winners[0]

    # Closes the SQLite connection.
    def close(self):
        with self.lock:
            self.connection.close()


# One live ocgcore duel: create cards, process, read messages, answer prompts.
class NativeDuel:
    # Wraps the engine handle and its DLL.
    def __init__(
        self,
        engine: "DuelEngine",
        handle,
        options: OCG_DuelOptions,
        callbacks: tuple,
        card_arrays: dict,
    ):
        self.engine = engine
        self.handle = handle
        self.options = options
        self.callbacks = callbacks
        self.card_arrays = card_arrays
        self.destroyed = False

    # Adds a card to the duel (deck/extra at setup time).
    def new_card(
        self,
        team: int,
        duelist: int,
        code: int,
        con: int,
        location: int,
        sequence: int = 0,
        position: int = 0,
    ):
        self._check_alive()
        info = OCG_NewCardInfo(team, duelist, code, con, location, sequence, position)
        self.engine.dll.OCG_DuelNewCard(self.handle, ctypes.byref(info))

    # Starts the duel (shuffles, draws the opening hands).
    def start(self):
        self._check_alive()
        self.engine.dll.OCG_StartDuel(self.handle)

    # Runs the engine one step; returns its status (END/AWAITING/CONTINUE).
    def process(self) -> int:
        self._check_alive()
        return int(self.engine.dll.OCG_DuelProcess(self.handle))

    # Fetches the raw output the last step produced.
    def get_message(self) -> bytes:
        self._check_alive()
        length = ctypes.c_uint32(0)
        pointer = self.engine.dll.OCG_DuelGetMessage(self.handle, ctypes.byref(length))
        if not pointer or length.value == 0:
            return b""
        return ctypes.string_at(pointer, length.value)

    # Same, parsed into dicts.
    def get_parsed_messages(self) -> list[dict]:
        raw_bytes = self.get_message()
        if not raw_bytes:
            return []
        try:
            return parse_messages(raw_bytes, strict=False)
        except ParseError:
            log.exception("Failed to parse OCG message bytes")
            return []

    # Hands the engine the answer to its pending prompt.
    def set_response(self, response: bytes):
        self._check_alive()
        if not isinstance(response, bytes):
            raise TypeError("response must be bytes")
        if not response:
            self.engine.dll.OCG_DuelSetResponse(self.handle, None, 0)
            return
        buffer = ctypes.create_string_buffer(response)
        self.engine.dll.OCG_DuelSetResponse(
            self.handle, ctypes.cast(buffer, ctypes.c_void_p), len(response)
        )

    # Raises if the duel was destroyed.
    def _check_alive(self):
        if self.destroyed or not self.handle:
            raise RuntimeError("This duel has already been destroyed")

    # Steps until the engine wants input or ends (bounded by max_steps); returns (status, bytes).
    def process_until_waiting(self, max_steps: int = 10000) -> tuple[int, bytes]:
        self._check_alive()
        if max_steps < 1:
            raise ValueError("max_steps must be positive")

        batches = []
        for _ in range(max_steps):
            status = self.process()
            payload = self.get_message()
            if payload:
                batches.append(payload)
            if status in (OCG_DUEL_STATUS_END, OCG_DUEL_STATUS_AWAITING):
                return status, b"".join(batches)
            if status != OCG_DUEL_STATUS_CONTINUE:
                raise RuntimeError(f"Unknown OCG_DuelProcess status: {status}")
        raise TimeoutError(f"ocgcore did not yield after {max_steps} steps")

    # Answers the prompt, then steps until the next prompt or the end.
    def respond_and_process(self, response: bytes, max_steps: int = 10000):
        self.set_response(response)
        return self.process_until_waiting(max_steps=max_steps)

    # Frees the engine's duel; safe to call twice.
    def destroy(self):
        if self.destroyed:
            return
        if self.handle:
            self.engine.dll.OCG_DestroyDuel(self.handle)
        self.handle = None
        self.destroyed = True
        self.card_arrays.clear()

    # Context-manager entry.
    def __enter__(self):
        self._check_alive()
        return self

    # Context-manager exit: destroys the duel.
    def __exit__(self, exc_type, exc_value, traceback):
        self.destroy()


# Loads ocgcore.dll and creates duels, supplying card data and Lua scripts through callbacks.
class DuelEngine:
    # Loads the DLL, opens the card database and binds the API.
    def __init__(
        self,
        dll_path: Path | str = DEFAULT_DLL,
        database_path: Path | str = DEFAULT_DATABASE,
        scripts_path: Path | str = DEFAULT_SCRIPTS,
        script_folders=DEFAULT_SCRIPT_FOLDERS,
        mingw_runtime: Optional[Path | str] = (r"C:\msys64\mingw64\bin"),
    ):
        self.dll_path = Path(dll_path).resolve()
        self.scripts_path = Path(scripts_path).resolve()
        self.script_folders = tuple(script_folders)

        if not self.dll_path.is_file():
            raise FileNotFoundError(f"OCG core DLL not found: {self.dll_path}")

        if not self.scripts_path.is_dir():
            raise FileNotFoundError(
                f"CardScripts folder not found: {self.scripts_path}"
            )

        self._dll_directory = None
        if mingw_runtime:
            runtime_path = Path(mingw_runtime)
            if runtime_path.is_dir() and hasattr(os, "add_dll_directory"):
                self._dll_directory = os.add_dll_directory(str(runtime_path))

        self.dll = ctypes.CDLL(str(self.dll_path))
        self._bind_api()
        self.database = CardDatabase(database_path)
        self.major, self.minor = self.get_version()
        log.info("Loaded OCG core version %d.%d", self.major, self.minor)

    # Declares ctypes signatures for every ocgcore function used.
    def _bind_api(self):
        dll = self.dll
        dll.OCG_GetVersion.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        dll.OCG_GetVersion.restype = None
        dll.OCG_CreateDuel.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(OCG_DuelOptions),
        ]
        dll.OCG_CreateDuel.restype = ctypes.c_int
        dll.OCG_DestroyDuel.argtypes = [ctypes.c_void_p]
        dll.OCG_DestroyDuel.restype = None
        dll.OCG_DuelNewCard.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(OCG_NewCardInfo),
        ]
        dll.OCG_DuelNewCard.restype = None
        dll.OCG_StartDuel.argtypes = [ctypes.c_void_p]
        dll.OCG_StartDuel.restype = None
        dll.OCG_DuelProcess.argtypes = [ctypes.c_void_p]
        dll.OCG_DuelProcess.restype = ctypes.c_int
        dll.OCG_DuelGetMessage.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        dll.OCG_DuelGetMessage.restype = ctypes.c_void_p
        dll.OCG_DuelSetResponse.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        dll.OCG_DuelSetResponse.restype = None
        dll.OCG_LoadScript.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
        ]
        dll.OCG_LoadScript.restype = ctypes.c_int

    # The core's (major, minor) version.
    def get_version(self) -> tuple[int, int]:
        major = ctypes.c_int()
        minor = ctypes.c_int()
        self.dll.OCG_GetVersion(ctypes.byref(major), ctypes.byref(minor))
        return major.value, minor.value

    # Builds the callback ocgcore uses to fetch card data by code.
    def _make_card_reader(self, arrays: dict):
        def read_card(payload, code, output):
            try:
                row = self.database.get_card(code)
                if row is None:
                    log.warning("Card ID %d is missing from cards.cdb", code)
                    return
                card = output.contents
                card.code = int(row["id"])
                card.alias = int(row["alias"] or 0)

                raw_level = int(row["level"] or 0)
                raw_defense = int(row["def"] or 0)

                card.type = int(row["type"] or 0) & 0xFFFFFFFF
                card.level = raw_level & 0xFF
                card.lscale = (raw_level >> 24) & 0xFF
                card.rscale = (raw_level >> 16) & 0xFF
                card.attribute = int(row["attribute"] or 0) & 0xFFFFFFFF
                card.race = int(row["race"] or 0) & 0xFFFFFFFFFFFFFFFF
                card.attack = int(row["atk"] or 0)

                if card.type & TYPE_LINK:
                    card.link_marker = raw_defense & 0xFFFFFFFF
                    card.defense = 0
                else:
                    card.link_marker = 0
                    card.defense = raw_defense

                setcodes = CardDatabase.unpack_setcodes(row["setcode"])
                array = (ctypes.c_uint16 * (len(setcodes) + 1))(*setcodes, 0)
                card.setcodes = ctypes.cast(array, ctypes.POINTER(ctypes.c_uint16))
                arrays[ctypes.addressof(card)] = array
            except Exception:
                log.exception("Card reader callback failed for ID %s", code)

        return OCG_DataReader(read_card)

    # Builds the callback ocgcore calls to release card data.
    @staticmethod
    def _make_card_reader_done(arrays: dict):
        def card_reader_done(payload, card_pointer):
            try:
                address = ctypes.addressof(card_pointer.contents)
                arrays.pop(address, None)
            except Exception:
                log.exception("Card reader cleanup callback failed")

        return OCG_DataReaderDone(card_reader_done)

    # Locates a Lua script in the configured script folders.
    def _find_script(self, script_name: str) -> Optional[Path]:
        if not script_name or Path(script_name).name != script_name:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.lua", script_name):
            return None

        root_file = self.scripts_path / script_name
        if root_file.is_file():
            return root_file

        for folder in self.script_folders:
            candidate = self.scripts_path / folder / script_name
            if candidate.is_file():
                return candidate
        return None

    # Builds the callback ocgcore uses to load Lua scripts.
    def _make_script_reader(self):
        def read_script(payload, duel_handle, name):
            try:
                if not name:
                    return 0
                script_name = name.decode("utf-8", errors="strict")
                script_path = self._find_script(script_name)

                if script_path is None:
                    if script_name != "c0.lua":
                        log.warning("Lua script not found: %s", script_name)
                    return 0

                script_bytes = script_path.read_bytes()
                name_bytes = script_name.encode("utf-8")
                result = self.dll.OCG_LoadScript(
                    duel_handle, script_bytes, len(script_bytes), name_bytes
                )
                if result == 0:
                    log.warning("OCG_LoadScript returned 0 for %s", script_name)
                return int(result)
            except Exception:
                log.exception("Script reader callback failed")
                return 0

        return OCG_ScriptReader(read_script)

    # Builds the callback receiving the engine's log lines.
    @staticmethod
    def _make_log_handler():
        def handle_log(payload, message, message_type):
            try:
                text = message.decode("utf-8", errors="replace") if message else ""
                log.info("[OCG:%d] %s", message_type, text)
            except Exception:
                log.exception("OCG log callback failed")

        return OCG_LogHandler(handle_log)

    # Creates a duel (random 256-bit seed, 8000 LP by default, 1 draw per turn) and loads the
    # base scripts.
    def create_duel(
        self,
        flags: int = 0,
        starting_lp: int = 8000,
        starting_hand: int = 5,
        draw_per_turn: int = 1,
        enable_unsafe_libraries: bool = False,
        seed: Optional[tuple[int, int, int, int]] = None,
    ) -> NativeDuel:

        if seed is None:
            seed = tuple(secrets.randbits(64) for _ in range(4))

        if len(seed) != 4:
            raise ValueError("seed must contain exactly four integers")

        seed = tuple(int(value) & 0xFFFFFFFFFFFFFFFF for value in seed)
        if not any(seed):
            seed = (1, 2, 3, 4)

        card_arrays = {}
        card_reader = self._make_card_reader(card_arrays)
        card_reader_done = self._make_card_reader_done(card_arrays)
        script_reader = self._make_script_reader()
        log_handler = self._make_log_handler()

        player = OCG_Player(starting_lp, starting_hand, draw_per_turn)
        options = OCG_DuelOptions(
            (ctypes.c_uint64 * 4)(*seed),
            int(flags) & 0xFFFFFFFFFFFFFFFF,
            player,
            player,
            card_reader,
            None,
            script_reader,
            None,
            log_handler,
            None,
            card_reader_done,
            None,
            int(bool(enable_unsafe_libraries)),
        )

        handle = ctypes.c_void_p()
        status = self.dll.OCG_CreateDuel(ctypes.byref(handle), ctypes.byref(options))

        if status != 0 or not handle.value:
            raise RuntimeError(f"OCG_CreateDuel failed with status {status}")

        duel = NativeDuel(
            self,
            handle,
            options,
            (card_reader, card_reader_done, script_reader, log_handler),
            card_arrays,
        )

        try:
            self.load_base_scripts(duel)
        except Exception:
            duel.destroy()
            raise

        return duel

    # Loads the constants/utility scripts every duel needs.
    def load_base_scripts(self, duel: NativeDuel):
        for script_name in ("constant.lua", "utility.lua"):
            script_path = self.scripts_path / script_name
            if not script_path.is_file():
                raise FileNotFoundError(
                    f"Required base Lua script missing: {script_path}"
                )

            script_bytes = script_path.read_bytes()
            result = self.dll.OCG_LoadScript(
                duel.handle,
                script_bytes,
                len(script_bytes),
                script_name.encode("utf-8"),
            )
            if result == 0:
                raise RuntimeError(f"OCG_LoadScript failed for {script_name}")
            log.info("Loaded base Lua script: %s", script_name)

    # Unloads the engine.
    def close(self):
        self.database.close()
        if self._dll_directory is not None:
            self._dll_directory.close()
            self._dll_directory = None


# A deck cannot be used in a duel (unknown row, bad size...).
class DuelDeckError(ValueError):
    pass


# Owns the live duels: converts YO3 decks to passcodes, creates duels and tracks them by id.
class DuelManager:
    # Binds the engine and the YO3 card table.
    def __init__(self, engine: DuelEngine, yo3_cards: dict):
        if not isinstance(engine, DuelEngine):
            raise TypeError("engine must be a DuelEngine")
        if not isinstance(yo3_cards, dict):
            raise TypeError("yo3_cards must be a dictionary")

        self.engine = engine
        self.yo3_cards = yo3_cards
        self._passcode_by_row = {}
        self.duels = {}
        self.lock = threading.RLock()

    # Turns big-endian u16 YO3 rows into OCG passcodes.
    def convert_rows(self, raw: bytes) -> list[int]:
        if not isinstance(raw, bytes):
            raise DuelDeckError("Deck section must be bytes")

        if len(raw) % 2:
            raise DuelDeckError("Incomplete 16-bit card row")

        if not raw:
            return []

        rows = struct.unpack(f">{len(raw) // 2}H", raw)
        result = []
        for row in rows:
            card = self.yo3_cards.get(row)
            if card is None:
                raise DuelDeckError(f"YO3 card row {row} does not exist")

            card_id = self._passcode_by_row.get(row)
            if card_id is None:
                card_id = self.engine.database.resolve_yo3_card(card)
                self._passcode_by_row[row] = card_id

            if not 0 < card_id <= 0xFFFFFFFF:
                raise DuelDeckError(f"Invalid card ID {card_id} for YO3 row {row}")

            result.append(card_id)

        return result

    # Splits a deck (bytes, dict or object) into main/extra/side YO3 row lists.
    def extract_deck_rows(self, deck):
        if isinstance(deck, (bytes, bytearray)):
            sections = {
                "main": bytes(deck),
                "extra": b"",
                "side": b"",
            }
        elif isinstance(deck, dict):
            sections = {
                "main": deck.get("main", b""),
                "extra": deck.get("extra", b""),
                "side": deck.get("side", b""),
            }
        else:
            sections = {
                "main": getattr(deck, "main", b""),
                "extra": getattr(deck, "extra", b""),
                "side": getattr(deck, "side", b""),
            }

        result = {}
        for name, raw in sections.items():
            if not isinstance(raw, (bytes, bytearray)):
                raise DuelDeckError(f"{name} Deck section must be bytes")
            raw = bytes(raw)
            if len(raw) % 2:
                raise DuelDeckError(f"Incomplete 16-bit card row in {name} Deck")
            result[name] = list(struct.unpack(f">{len(raw) // 2}H", raw))

        return result

    # Converts and validates a deck (main 1-60, extra and side up to 15).
    def convert_deck(self, deck):
        if isinstance(deck, (bytes, bytearray)):
            main_raw = bytes(deck)
            extra_raw = b""
            side_raw = b""
        elif isinstance(deck, dict):
            main_raw = deck.get("main", b"")
            extra_raw = deck.get("extra", b"")
            side_raw = deck.get("side", b"")
        else:
            main_raw = getattr(deck, "main", b"")
            extra_raw = getattr(deck, "extra", b"")
            side_raw = getattr(deck, "side", b"")

        main = self.convert_rows(main_raw)
        extra = self.convert_rows(extra_raw)
        side = self.convert_rows(side_raw)

        if not main:
            raise DuelDeckError("Main Deck is empty")
        if len(main) > 60:
            raise DuelDeckError("Main Deck exceeds 60 cards")
        if len(extra) > 15:
            raise DuelDeckError("Extra Deck exceeds 15 cards")
        if len(side) > 15:
            raise DuelDeckError("Side Deck exceeds 15 cards")

        return {"main": main, "extra": extra, "side": side}

    # Builds a native duel for two players from their decks and registers it under a new id.
    def create_duel(
        self,
        player1_id: int,
        player2_id: int,
        deck1,
        deck2,
        npc_player_id: Optional[int] = None,
        **engine_options,
    ):
        player1_id = int(player1_id)
        player2_id = int(player2_id)

        if player1_id <= 0 or player2_id <= 0:
            raise ValueError("Player IDs must be positive")
        if player1_id == player2_id:
            raise ValueError("A player cannot duel themselves")

        if npc_player_id is not None:
            npc_player_id = int(npc_player_id)
            if npc_player_id not in (player1_id, player2_id):
                raise ValueError("NPC player ID must match one of the duel players")

        original_deck1 = deck1
        original_deck2 = deck2
        original_rows1 = self.extract_deck_rows(deck1)
        original_rows2 = self.extract_deck_rows(deck2)
        converted1 = self.convert_deck(deck1)
        converted2 = self.convert_deck(deck2)

        with self.lock:
            duel_id = secrets.token_hex(8)
            while duel_id in self.duels:
                duel_id = secrets.token_hex(8)

            native = self.engine.create_duel(**engine_options)

            LOCATION_DECK = 0x01
            LOCATION_EXTRA = 0x40
            POS_FACEDOWN_DEFENSE = 0x08

            try:
                for team, converted in ((0, converted1), (1, converted2)):
                    # Every duel gets its own order: the deck goes into the engine
                    # shuffled here, so the opening hand never depends on whether the
                    # engine shuffled (the rows are matched back by passcode later).
                    shuffled_main = list(converted["main"])
                    secrets.SystemRandom().shuffle(shuffled_main)
                    for sequence, code in enumerate(shuffled_main):
                        native.new_card(
                            team=team,
                            duelist=0,
                            code=code,
                            con=team,
                            location=LOCATION_DECK,
                            sequence=sequence,
                            position=POS_FACEDOWN_DEFENSE,
                        )
                    for sequence, code in enumerate(converted["extra"]):
                        native.new_card(
                            team=team,
                            duelist=0,
                            code=code,
                            con=team,
                            location=LOCATION_EXTRA,
                            sequence=sequence,
                            position=POS_FACEDOWN_DEFENSE,
                        )

                native.start()
                status, initial_messages = native.process_until_waiting()

                if status not in (OCG_DUEL_STATUS_AWAITING, OCG_DUEL_STATUS_END):
                    raise RuntimeError(f"Unexpected initial duel status: {status}")

            except Exception:
                native.destroy()
                raise

            npc_seat = None
            if npc_player_id is not None:
                npc_seat = 0 if player1_id == npc_player_id else 1

            self.duels[duel_id] = {
                "duel_id": duel_id,
                "players": (player1_id, player2_id),
                "decks": (converted1, converted2),
                "original_decks": (original_deck1, original_deck2),
                "original_deck_rows": (original_rows1, original_rows2),
                "native": native,
                "started": True,
                "ocg_status": status,
                "ocg_messages": initial_messages,
                "awaiting_response": (status == OCG_DUEL_STATUS_AWAITING),
                "ended": (status == OCG_DUEL_STATUS_END),
                "npc_player_id": npc_player_id,
                "npc_seat": npc_seat,
                "npc_controlled": (npc_player_id is not None),
                "initial_processed": True,
            }

        log.info(
            "Created native duel %s for players %d and %d; NPC=%s seat=%s",
            duel_id,
            player1_id,
            player2_id,
            npc_player_id,
            npc_seat,
        )
        return duel_id

    # Steps a registered duel until it needs an answer.
    def process_duel(self, duel_id, max_steps: int = 10000):
        with self.lock:
            duel = self.duels.get(duel_id)
            if duel is None:
                raise KeyError(f"Unknown duel: {duel_id}")
            native = duel["native"]

        status, messages = native.process_until_waiting(max_steps=max_steps)
        with self.lock:
            duel["ocg_status"] = status
            duel["ocg_messages"] = messages
            duel["awaiting_response"] = status == OCG_DUEL_STATUS_AWAITING
            duel["ended"] = status == OCG_DUEL_STATUS_END

        return status, messages

    # Answers a registered duel's prompt and steps on.
    def respond_duel(self, duel_id, response: bytes, max_steps: int = 10000):
        with self.lock:
            duel = self.duels.get(duel_id)
            if duel is None:
                raise KeyError(f"Unknown duel: {duel_id}")
            if not duel.get("awaiting_response", False):
                raise RuntimeError("Duel is not waiting for a player response")
            native = duel["native"]

        status, messages = native.respond_and_process(response, max_steps=max_steps)
        with self.lock:
            duel["ocg_status"] = status
            duel["ocg_messages"] = messages
            duel["awaiting_response"] = status == OCG_DUEL_STATUS_AWAITING
            duel["ended"] = status == OCG_DUEL_STATUS_END

        return status, messages

    # A registered duel's record, or None.
    def get_duel(self, duel_id):
        with self.lock:
            return self.duels.get(duel_id)

    # (duel_id, duel) the player is assigned to, or None.
    def find_duel_for_player(self, player_id):
        player_id = int(player_id)
        with self.lock:
            for duel_id, duel in self.duels.items():
                if player_id in duel["players"]:
                    return duel_id, duel
        return None

    # Destroys a duel and forgets it.
    def remove_duel(self, duel_id):
        with self.lock:
            duel = self.duels.pop(duel_id, None)

        if duel is None:
            return False

        native = duel.get("native")
        if native is not None:
            native.destroy()

        log.info("Destroyed native duel %s", duel_id)
        return True

    # Destroys every duel.
    def close(self):
        with self.lock:
            duel_ids = list(self.duels)

        for duel_id in duel_ids:
            self.remove_duel(duel_id)

        log.info("Duel manager closed")


# Front-server side of hosted duels: registers challenges and creates duels when accepted.
class FrontMatchmakingRouter:
    # Binds the duel manager and the address clients should use for the duel server.
    def __init__(
        self,
        duel_manager: DuelManager,
        default_duel_host: str,
        default_duel_port: int,
    ):
        if not isinstance(duel_manager, DuelManager):
            raise TypeError("duel_manager must be a DuelManager")

        if not default_duel_host:
            raise ValueError("A duel host address is required")

        port = int(default_duel_port)
        if not 1 <= port <= 65535:
            raise ValueError("Invalid duel port")

        self.duel_manager = duel_manager
        self.default_duel_host = str(default_duel_host)
        self.default_duel_port = port

        self.lock = threading.RLock()
        self.challenges = {}
        self.player_challenges = {}

        log.info(
            "Front matchmaking router initialized: %s:%d",
            self.default_duel_host,
            self.default_duel_port,
        )

    # A host puts up a challenge (replacing their previous one); False if they are already in a
    # duel.
    def handle_host_challenge(self, player_id, fields):
        player_id = int(player_id)

        if player_id <= 0:
            raise ValueError("Invalid player ID")

        if not isinstance(fields, dict):
            raise TypeError("Challenge fields must be a dictionary")

        if self.duel_manager.find_duel_for_player(player_id):
            log.warning(
                "Player %d cannot host a challenge while in a duel",
                player_id,
            )
            return False

        with self.lock:
            old_host = self.player_challenges.get(player_id)

            if old_host is not None:
                self.challenges.pop(old_host, None)

            self.challenges[player_id] = {
                "host_id": player_id,
                "fields": dict(fields),
                "created": time.time(),
            }

            self.player_challenges[player_id] = player_id

        log.info("Player %d registered a duel challenge", player_id)
        return True

    # A challenger answers a host's challenge; on accept creates the duel and returns its id.
    def handle_accept_reject_challenge(
        self,
        player_id,
        host_id,
        accept,
        deck_payload,
    ):
        player_id = int(player_id)
        host_id = int(host_id)

        if player_id <= 0 or host_id <= 0:
            return None

        if player_id == host_id:
            log.warning("Player cannot accept their own challenge")
            return None

        with self.lock:
            challenge = self.challenges.get(host_id)

            if challenge is None:
                log.warning(
                    "Player %d attempted to respond to nonexistent "
                    "challenge from %d",
                    player_id,
                    host_id,
                )
                return None

            if not accept:
                self.challenges.pop(host_id, None)
                self.player_challenges.pop(host_id, None)

                log.info(
                    "Player %d rejected challenge from %d",
                    player_id,
                    host_id,
                )
                return None

            if self.duel_manager.find_duel_for_player(host_id):
                log.warning("Host %d is already in a duel", host_id)
                return None

            if self.duel_manager.find_duel_for_player(player_id):
                log.warning("Player %d is already in a duel", player_id)
                return None

            host_session = STATE.online.get(host_id)

            if host_session is None:
                log.warning("Challenge host %d is offline", host_id)
                self.challenges.pop(host_id, None)
                self.player_challenges.pop(host_id, None)
                return None

            host_player = host_session.player()

            if host_player is None:
                return None

            host_decks = host_player.get("decks", [])

            if host_decks:
                selected_deck = host_decks[0]

                host_deck = {
                    "main": list(selected_deck.main),
                    "extra": list(selected_deck.extra),
                    "side": list(selected_deck.side),
                }
            else:
                if GAME.starter_deck is None:
                    log.warning("Host %d has no usable deck", host_id)
                    return None

                host_deck = preset_payload(GAME.starter_deck)

            if not isinstance(deck_payload, dict):
                log.warning(
                    "Invalid challenger deck payload from %d",
                    player_id,
                )
                return None

            try:
                duel_id = self.duel_manager.create_duel(
                    player1_id=host_id,
                    player2_id=player_id,
                    deck1=host_deck,
                    deck2=deck_payload,
                )
            except Exception:
                log.exception(
                    "Failed to create duel between %d and %d",
                    host_id,
                    player_id,
                )
                return None

            self.challenges.pop(host_id, None)
            self.player_challenges.pop(host_id, None)
            self.player_challenges.pop(player_id, None)

            log.info(
                "Matchmaking created duel %s: %d vs %d",
                duel_id,
                host_id,
                player_id,
            )

            return duel_id

    # Frames the 0x1839 message announcing a duel's server address.
    def build_connection_packet(self, duel_id):
        duel = self.duel_manager.get_duel(duel_id)

        if duel is None:
            raise KeyError(f"Unknown duel: {duel_id}")

        fields, items = duel_announce_payload(
            self.default_duel_host,
            self.default_duel_port,
            zlib.crc32(duel_id.encode("ascii")) & 0x7FFFFFFF,
            "Duelist",
        )
        message = make_reply(0x1839, fields, items)
        return encode_frame(message)

    # Drops a player's challenges (they went away).
    def remove_player(self, player_id):
        player_id = int(player_id)

        with self.lock:
            host_id = self.player_challenges.pop(player_id, None)

            if host_id is not None:
                self.challenges.pop(host_id, None)

            self.challenges.pop(player_id, None)

    # Forgets every challenge.
    def close(self):
        with self.lock:
            self.challenges.clear()
            self.player_challenges.clear()

        log.info("Front matchmaking router closed")


# Atomic pickle persistence for the server state.
class ServerStore:

    # Reads a state file; a missing, unreadable or non-dict file logs an error and gives {}.
    @staticmethod
    def read_or_keep(path: Path, what: str) -> dict:
        if not path.is_file():
            return {}

        try:
            with path.open("rb") as f:
                value = pickle.load(f)

            if not isinstance(value, dict):
                log.error("%s NOT LOADED: stored value is not a dictionary", what)
                return {}

            return value

        except Exception as e:
            log.error("%s NOT LOADED: %s", what, e)
            return {}

    # Writes the state to a .tmp file, fsyncs and atomically renames it over the real one.
    @staticmethod
    def write(path: Path, value: dict):
        if not isinstance(value, dict):
            raise TypeError("ServerStore.write expects a dictionary")

        path.parent.mkdir(exist_ok=True, parents=True)

        tmp = path.with_name(path.name + ".tmp")

        try:
            with tmp.open("wb") as f:
                pickle.dump(
                    value,
                    f,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp, path)

        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


# BP awarded for a duel result: 100 for a win, 50 for a draw, 30 otherwise.
def duel_bp(result: int = 1) -> int:
    return {
        1: 100,
        3: 50,
    }.get(int(result), 30)


# A booster pack on the shop shelf.
@dataclass
class ShopPack:
    id: int
    released: str = ITEM_DATE
    modified: str = ITEM_DATE
    tiers: list = field(default_factory=list)
    pack_type: int = 1
    image: str = "noImage"
    name_id: int = 0
    description_id: int = 0


# A structure deck on the shop shelf.
@dataclass
class ShopDeck:
    released: str = ITEM_DATE
    modified: str = ITEM_DATE
    deck: object = None


# Splits a pack's card rows into tiers: a new tier starts wherever the row numbers stop rising.
def split_tiers(cards):
    tiers = []

    for row in cards:
        if not tiers or row <= tiers[-1][-1]:
            tiers.append([row])
        else:
            tiers[-1].append(row)

    return tiers


# Opens one pack: COMMONS (4) random cards of the first tier plus one from a random higher tier;
# returns CARD rows.
def open_shop_pack(pack, rng_seed=0):
    if pack is None or not pack.tiers:
        return []

    first = pack.tiers[0]

    if not first:
        return []

    cards = [first[secrets.randbelow(len(first))] for _ in range(COMMONS)]

    higher = [tier for tier in pack.tiers[1:] if tier]

    if not higher:
        higher = [first]

    tier = higher[secrets.randbelow(len(higher))]

    cards.append(tier[secrets.randbelow(len(tier))])

    return cards


# A pack as a shop shelf entry (0x1C22).
def shelf_item(pack):
    return MessageRecord(
        fields={
            "item_type": 0,
            "item_id": int(pack.id),
            "item_cost": PACK_COST,
            "item_max": PACK_MAX,
            "sale_id": int(pack.id),
            "sale": 0,
            "item_trade": 0,
            "modified": pack.modified,
            "rel_time": pack.released,
        }
    )


# A structure deck as a shop shelf entry.
def shelf_deck(deck):
    if deck is None or deck.deck is None:
        raise ValueError("Invalid structure deck")

    deck_id = int(deck.deck.deck_id)

    return MessageRecord(
        fields={
            "item_type": 1,
            "item_id": deck_id,
            "item_cost": DECK_COST,
            "item_max": DECK_MAX,
            "sale_id": DECK_SALE + deck_id,
            "sale": 0,
            "item_trade": 0,
            "modified": deck.modified,
            "rel_time": deck.released,
        }
    )


# A shop shelf entry for any item type (id, price, dates).
def shelf_entry(item_type, id_val, cost):
    item_type = int(item_type)
    id_val = int(id_val)
    cost = int(cost)

    if id_val <= 0:
        raise ValueError("Shop item ID must be positive")

    if cost < 0:
        raise ValueError("Shop item cost cannot be negative")

    return MessageRecord(
        fields={
            "item_type": item_type,
            "item_id": id_val,
            "item_param": id_val,
            "item_cost": cost,
            "item_max": 1,
            "sale_id": ITEM_SALE + id_val,
            "sale": 0,
            "item_trade": 0,
            "modified": ITEM_DATE,
            "rel_time": ITEM_DATE,
        }
    )


TAG_RECORD = 3
TAG_DECK = 6


# A nested wire record: fields, child items and a tag (record 3, deck 6, ...).
@dataclass
class MessageRecord:
    fields: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    tag: int = TAG_RECORD


# Fields and items of the 0x1839 duel announcement (server address, avatars, rule).
def duel_announce_payload(host, port, duel_id, name, avatar_1=b"", avatar_2=b""):
    fields = {
        "duel_id": duel_id,
        "avatar_1": avatar_1,
        "name": name,
        "avatar_2": avatar_2,
        "card_protector_1": 0,
        "card_protector_2": 0,
        "duel_match_type": 4,
        "match_type": 4,
        "time_total": DUEL_TOTAL_TIME,
        "time_turn": DUEL_TURN_TIME,
        "host": host,
        "port": port,
    }
    return fields, [duel_data_item(host, port)]


# The `duelData` record (host, port, timers, rule) the client reads to join a duel.
def duel_data_item(host, port):
    return MessageRecord(
        fields={
            "name": "duelData",
            "host": host,
            "port": port,
            "time_total": DUEL_TOTAL_TIME,
            "time_turn": DUEL_TURN_TIME,
        },
        items=[
            MessageRecord(
                fields={
                    "name": "rule",
                    "match_type": 1,
                    "time_total": DUEL_TOTAL_TIME,
                    "time_turn": DUEL_TURN_TIME,
                }
            )
        ],
    )


# One saved deck: id, name and main/extra/side rows (big-endian u16).
@dataclass
class Deck:
    deck_id: int = 0
    deck_name: str = ""
    main: bytes = b""
    extra: bytes = b""
    side: bytes = b""

    # The deck as a wire record (tag 6).
    def record(self):
        return MessageRecord(
            fields={
                "deck_id": self.deck_id,
                "deck_name": self.deck_name,
                "discription": "",
                "card_protector": 0,
                "main": self.main,
                "extra": self.extra,
                "side": self.side,
            },
            tag=TAG_DECK,
        )

    # Builds a deck from a client record, clamping names and lengths.
    @classmethod
    def from_record(cls, record):
        fields = record.get("fields", {})

        def get_bytes(name):
            value = fields.get(name, b"")
            if not isinstance(value, bytes):
                return b""
            return value[: MAX_DECK_ROWS * 2]

        name = fields.get("deck_name", "")
        if not isinstance(name, str):
            name = ""

        return cls(
            deck_id=int(fields.get("deck_id", 0)),
            deck_name=name[:MAX_DECK_NAME],
            main=get_bytes("main"),
            extra=get_bytes("extra"),
            side=get_bytes("side"),
        )


# A fresh player character with default stats, empty collections and speech slots.
def make_player(player_id=1, player_name="Duelist", player_index=0):
    player = {
        "pname": player_name,
        "pindex": player_index,
        "player_id": player_id,
        "dlv": 1,
        "title": 0,
        "n_win": 0,
        "n_lose": 0,
        "n_draw": 0,
        "n_duelall": 0,
        "team_level": 0,
        "team_name": "",
        "team_authority": 0,
        "country": "GB",
        "avatar1": b"\x00" * 82,
        "pbin": b"\x00" * 512,
        "comment00": "",
        "comment01": "",
        "decks": [],
        "cards": {},
        "given": {},
        "eval_num": 0,
        "eval_total": 0,
    }
    for i in range(23):
        player[f"speech{i:02d}"] = ""
    return player


def eval_level(player):
    """Manner level 0-4: (average + 10) / 5, 2 (the middle) without ratings."""
    num = int(player.get("eval_num", 0) or 0)
    if num <= 0:
        return 2
    average = int(player.get("eval_total", 0) or 0) / num
    return max(0, min(4, round((average + 10) / 5)))


def player_pbin(player):
    """The owned-items bitmap: exactly 512 bytes (4096 bits), read unchecked.

    Bit 0x4B*8 is set so MainGamen skips the Online 2 takeover step, bits
    0x250-0x253 let Kaiba, Marik, Alexis and Jack duel, and every bought
    protector or avatar item sets its own pbin id's bit.
    """
    bits = bytearray(bytes(player.get("pbin") or b"")[:512].ljust(512, b"\x00"))
    bits[0x4B] |= 0x01
    bits[74] |= 0x0F
    for item_id in player.get("pbin_items", ()):
        if isinstance(item_id, int) and 0 <= item_id < 4096:
            bits[item_id >> 3] |= 1 << (item_id & 7)
    return bytes(bits)


# A player as the client's record (strips server-only keys, adds pbin, team, evaluation).
def make_player_record(player, country=None):
    if "record" in LEGACY:
        original = {
            "pname",
            "pindex",
            "player_id",
            "dlv",
            "title",
            "n_win",
            "n_lose",
            "n_draw",
            "n_duelall",
            "team_level",
            "team_name",
            "team_authority",
            "country",
            "avatar1",
            "pbin",
            "comment00",
            "comment01",
        } | {f"speech{i:02d}" for i in range(23)}
        return {k: v for k, v in player.items() if k in original}
    allowed_fields = {
        "pname",
        "pindex",
        "player_id",
        "dlv",
        "title",
        "n_win",
        "n_lose",
        "n_draw",
        "n_duelall",
        "team_level",
        "team_name",
        "team_authority",
        "country",
        "avatar1",
        "comment00",
        "comment01",
    }
    for i in range(23):
        allowed_fields.add(f"speech{i:02d}")
    record = {key: value for key, value in player.items() if key in allowed_fields}

    if country:
        record["country"] = country

    avatar = player.get("avatar1")
    record["avatar1"] = avatar if isinstance(avatar, bytes) and avatar else b"\x00" * 82
    record["pbin"] = player_pbin(player)
    npcbin = player.get("npcbin")
    record["npcbin"] = (
        bytes(npcbin)[:2048].ljust(2048, b"\x00")
        if isinstance(npcbin, (bytes, bytearray))
        else b"\x00" * 2048
    )

    record["evalnum"] = max(0, int(player.get("eval_num", 0) or 0))
    record["evaltotal"] = max(0, int(player.get("eval_total", 0) or 0))
    record["evallevel"] = eval_level(player)

    team_id = STATE.player_teams.get(player.get("player_id"))
    team = STATE.teams.get(team_id) if team_id else None
    if team is not None:
        record["team_level"] = 1
        record["team_name"] = team.get("name", "")
        record["team_authority"] = team.get("members", {}).get(
            player.get("player_id"), 0
        )
    return record


def friend_list_items(player_id):
    """0x251C items: {friend_id, friend_name, login_flag}."""
    items = []
    for friend_id in sorted(STATE.friends.get(player_id, ())):
        friend = STATE.player(friend_id)
        if friend is not None:
            items.append(
                {
                    "friend_id": friend_id,
                    "friend_name": friend["pname"],
                    "login_flag": 1 if friend_id in STATE.online else 0,
                }
            )
    return items


def ignore_list_items(player_id):
    """0x252A items: {black_id, black_name, login_flag}."""
    items = []
    for black_id in sorted(STATE.ignored.get(player_id, ())):
        black = STATE.player(black_id)
        if black is not None:
            items.append(
                {
                    "black_id": black_id,
                    "black_name": black["pname"],
                    "login_flag": 1 if black_id in STATE.online else 0,
                }
            )
    return items


def notify_friend_presence(player_id):
    """Whenever a player logs in or out, their online friends get their list again."""
    for friend_id in list(STATE.friends.get(player_id, ())):
        if friend_id in STATE.online:
            STATE.online_push(friend_id, 0x251C, items=friend_list_items(friend_id))


# (team_id, team) of a player, or (None, None).
def team_of(player_id):
    team_id = STATE.player_teams.get(player_id)
    team = STATE.teams.get(team_id) if team_id else None
    return (team_id, team) if team is not None else (None, None)


# A stored mail as the client's D-Mail record.
def mail_record(mail):
    sent = mail.get("send_date")
    if not sent:
        stamp = mail.get("timestamp", int(time.time()))
        sent = datetime.fromtimestamp(stamp, JST).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "mail_id": int(mail.get("mail_id", 0)),
        "mail_type": int(mail.get("mail_type", 0)),
        "from_id": int(mail.get("from_id", 0)),
        "from_name": mail.get("from_name", ""),
        "to_id": int(mail.get("to_id", 0)),
        "to_name": mail.get("to_name", ""),
        "title": mail.get("title", ""),
        "body": mail.get("body", ""),
        "send_date": sent,
        "team_id": int(mail.get("team_id", 0)),
        "team_name": mail.get("team_name", ""),
    }


def new_present(kind, **extra):
    """A present for 0x2777: PresentWindow reads these fields (see Shop)."""
    present = {
        "present_id": secrets.randbelow(0x7FFFFFFF) + 1,
        "type": kind,
        "level": 0,
        "form": 0,
        "item_img": "",
        "end_time": "",
        "item": b"",
        "pack_id": 0,
        "name_id": 0,
        "desc": "",
    }
    present.update(extra)
    return present


# A login account: user id, name, Duel Points, premium flag and its player characters.
class Account:
    # Starts with one default player.
    def __init__(self):
        self.user_id = USER_ID
        self.username = USERNAME
        self.duel_points = DUEL_POINTS
        self.premium = PREMIUM
        self.players = [make_player()]

    # The player character with this id, or None.
    def find_player(self, player_id):
        return next(
            (player for player in self.players if player["player_id"] == player_id),
            None,
        )


# All shared server data: accounts, social graph, teams, rentals, events and live sessions.
class ServerState:
    # Empty state; see the field comments below.
    def __init__(self):
        self.accounts = {}
        self.friends = {}
        self.ignored = {}
        self.friend_requests = {}
        self.mailboxes = {}
        self.teams = {}
        self.player_teams = {}
        self.chat_rooms = {}
        self.events = []
        self.online = {}
        self.ratings = {}

        self.next_mail_id = 1
        self.next_team_id = 1
        self.next_event_id = 100
        self.next_message_id = 1

        # Persistent: card rentals (one dict per rental, see rental_wire)
        # and the user events (tournaments) that are still open.
        self.rentals = []
        self.next_rental_id = 1
        self.user_events = {}

        # Transient (never saved, they die with the connections):
        # pending team invitations {invitee: inviter}, pending trade and
        # rental invitations {kind: {invitee: inviter}}, the trade or rental
        # scene each player sits in {player_id: scene}, the manner
        # evaluations asked for {eval_id: {...}} and the finished events
        # the lobby can still ask about {event_id: event}.
        self.team_invites = {}
        self.scene_invites = {"trade": {}, "rental": {}}
        self.scenes = {}
        self.evaluations = {}
        self.next_eval_id = 1
        self.finished_events = {}

    # The account for a user id, created on first use.
    def get_account(self, user_id):
        if user_id not in self.accounts:
            account = Account()
            account.user_id = user_id
            self.accounts[user_id] = account
        return self.accounts[user_id]

    # Writes the persistent part of the state to disk.
    def save(self):
        state = {
            "accounts": self.accounts,
            "friends": self.friends,
            "ignored": self.ignored,
            "friend_requests": self.friend_requests,
            "mailboxes": self.mailboxes,
            "teams": self.teams,
            "player_teams": self.player_teams,
            "events": self.events,
            "ratings": self.ratings,
            "next_mail_id": self.next_mail_id,
            "next_team_id": self.next_team_id,
            "next_event_id": self.next_event_id,
            "next_message_id": self.next_message_id,
            "rentals": self.rentals,
            "next_rental_id": self.next_rental_id,
            # Only the open events survive; their runtime part is dropped.
            "user_events": {
                event_id: {k: v for k, v in event.items() if k != "run"}
                for event_id, event in self.user_events.items()
                if event.get("status") == USER_EVENT_OPEN
            },
        }
        ServerStore.write(STATE_FILE, state)

    # Reads the saved state back (open events only; running ones cannot survive a restart).
    def load(self):
        if not STATE_FILE.exists():
            return
        state = ServerStore.read_or_keep(STATE_FILE, "server state")
        if state:
            for key, value in state.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            self.friend_requests = {}
            # A running event cannot come back (its duels and the players'
            # lobbies died with the server): keep the open ones only.
            self.user_events = {
                event_id: event
                for event_id, event in self.user_events.items()
                if event.get("status") == USER_EVENT_OPEN
            }
            log.info("Persistent server state loaded")

    # The player with this id across all accounts, or None.
    def player(self, player_id):
        for account in self.accounts.values():
            player = account.find_player(player_id)
            if player is not None:
                return player
        return None

    # The account owning this player id, or None.
    def account_for_player(self, player_id):
        for account in self.accounts.values():
            if account.find_player(player_id):
                return account
        return None

    # Queues a message to a player's online session (no-op if offline).
    def online_push(self, player_id, request_id, fields=None, items=None):
        session = self.online.get(player_id)
        if session is None or session.closed:
            return
        try:
            asyncio.create_task(session.send_message(request_id, fields, items))
        except RuntimeError:
            pass


STATE = ServerState()
STATE.load()


# ===========================================================================
# Teams (the community agent's modes 5-12, Team window 0x4BBEC0)
#
# A team is {"name", "created", "members": {player_id: authority}}, kept in
# STATE.teams; STATE.player_teams maps each member back to the team id.
# Authority: 2 leader, 1 subleader, 0 member (dicts keep the join order).
# ===========================================================================


def valid_team_name(name):
    """A team name is 1-16 printable characters without spaces at the edges."""
    return (
        isinstance(name, str)
        and name == name.strip()
        and 1 <= len(name) <= TEAM_NAME_MAX
        and all(ch.isprintable() for ch in name)
    )


def team_name_taken(name):
    """Names are unique regardless of case ("millennium" == "Millennium")."""
    wanted = name.casefold()
    return any(t.get("name", "").casefold() == wanted for t in STATE.teams.values())


def team_push(team, request_id, fields=None, items=None, skip=()):
    """Pushes one message to every online member of the team except `skip`."""
    for member_id in list(team["members"]):
        if member_id not in skip:
            STATE.online_push(member_id, request_id, fields, items)


def team_drop_member(player_id):
    """Takes a player out of their team.

    A leader who goes hands the leadership to the first subleader (else the
    longest-standing member); a team left without members is deleted.
    """
    team_id = STATE.player_teams.pop(player_id, None)
    team = STATE.teams.get(team_id)
    if team is None:
        return
    was_leader = team["members"].pop(player_id, None) == TEAM_LEADER
    if not team["members"]:
        del STATE.teams[team_id]
    elif was_leader:
        successor = next(
            (pid for pid, auth in team["members"].items() if auth == TEAM_SUBLEADER),
            next(iter(team["members"])),
        )
        team["members"][successor] = TEAM_LEADER
    STATE.save()


def team_disband(team_id):
    """Deletes a team and releases every member (the leader's Disband)."""
    team = STATE.teams.pop(team_id, None)
    if team is None:
        return
    for member_id in team["members"]:
        STATE.player_teams.pop(member_id, None)
    STATE.save()


# ===========================================================================
# Invitations and scenes (trade and rental)
#
# Both scenes follow one pattern: Player Information invites, the other
# player answers from their bell, a yes opens the scene on both clients.
# The ids differ per kind, so they sit in one table.
# ===========================================================================

SCENE_IDS = {
    "trade": {
        "invite_push": 0x2BC1,
        "invite_ok": 0x2BD7,
        "invite_err": 0x2BD8,
        "cancel_push": 0x2BC5,
        "cancel_ok": 0x2BDD,
        "answer_push": 0x2BC3,
        "answer_ok": 0x2BDB,
        "answer_err": 0x2BDC,
        "ready_ok": 0x2BED,
        "ready_err": 0x2BEE,
        "start_push": 0x2BD6,
        "leave_ok": 0x2BE9,
        "leave_push": 0x2BCD,
    },
    "rental": {
        "invite_push": 0x2C25,
        "invite_ok": 0x2D50,
        "invite_err": 0x2D51,
        "cancel_push": 0x2C29,
        "cancel_ok": 0x2D56,
        "answer_push": 0x2C27,
        "answer_ok": 0x2D54,
        "answer_err": 0x2D55,
        "ready_ok": 0x2D68,
        "ready_err": 0x2D69,
        "start_push": 0x2C3F,
        "leave_ok": 0x2D6A,
        "leave_push": 0x2C37,
    },
}


def drop_invites(player_id):
    """Forgets every invitation the player made or was sent (they left).

    Invitations they made also vanish from the invited players' bells.
    """
    for invitee, inviter in list(STATE.team_invites.items()):
        if player_id in (invitee, inviter):
            del STATE.team_invites[invitee]
            if inviter == player_id:
                STATE.online_push(invitee, 0x2B03)
    for kind, invites in STATE.scene_invites.items():
        for invitee, inviter in list(invites.items()):
            if player_id in (invitee, inviter):
                del invites[invitee]
                if inviter == player_id:
                    STATE.online_push(invitee, SCENE_IDS[kind]["cancel_push"])


def scene_partner(scene, player_id):
    """The other player of a two-player scene."""
    first, second = scene["players"]
    return second if player_id == first else first


def scene_end(scene, notify=None):
    """Closes a scene for both players; `notify` (a player) is told it ended."""
    for player_id in scene["players"]:
        if STATE.scenes.get(player_id) is scene:
            del STATE.scenes[player_id]
    if notify is not None:
        STATE.online_push(notify, SCENE_IDS[scene["kind"]]["leave_push"])


def new_scene(kind, inviter, answerer):
    """Opens a scene; by convention the inviter is first (the lender)."""
    scene = {"kind": kind, "players": (inviter, answerer), "ready": set(), "started": False}
    if kind == "trade":
        scene.update(offers={inviter: [], answerer: []}, ok=set(), final={})
    else:
        scene.update(type=0, days=1, main=b"", extra=b"", side=b"", confirmed=False)
    STATE.scenes[inviter] = STATE.scenes[answerer] = scene
    return scene


# --- Collections: what a player can offer ----------------------------------


def rows_of(raw):
    """Decodes a list of big-endian u16 CARD_Prop rows (as sent by the client)."""
    raw = bytes(raw or b"")
    count = len(raw) // 2
    return list(struct.unpack(f">{count}H", raw[: count * 2]))


def rental_active(rental):
    """A rental runs until its confirm flags turn 1."""
    return not rental.get("confirm_from")


def lent_counts(player_id):
    """The cards (row -> count) the player has lent out and not got back."""
    lent = Counter()
    for rental in STATE.rentals:
        if rental["from_id"] == player_id and rental_active(rental):
            for part in ("main", "extra", "side"):
                lent.update(rows_of(rental.get(part)))
    return lent


def tradable_counts(player_id):
    """What the player may offer: owned cards less the ones they lent."""
    player = STATE.player(player_id)
    if player is None:
        return Counter()
    return Counter(player_collection(player)) - lent_counts(player_id)


def execute_trade(first_id, first_rows, second_id, second_rows):
    """Moves both offers between the two players' collections.

    The giver's `given` count rises (the collection is starter + `cards`
    less `given`), the taker's `cards` count rises. Returns False, changing
    nothing, when either side no longer owns its offer.
    """
    first_offer, second_offer = Counter(first_rows), Counter(second_rows)
    if first_offer - tradable_counts(first_id) or second_offer - tradable_counts(second_id):
        return False
    first, second = STATE.player(first_id), STATE.player(second_id)
    if first is None or second is None:
        return False
    for giver, taker, offer in ((first, second, first_offer), (second, first, second_offer)):
        given, cards = giver.setdefault("given", {}), taker.setdefault("cards", {})
        for row, count in offer.items():
            given[row] = given.get(row, 0) + count
            cards[row] = cards.get(row, 0) + count
    STATE.save()
    return True


# --- Rental records ----------------------------------------------------------


def rental_wire(rental):
    """A rental as the client's record 0x2D6C / 0x2D6E item."""
    return {
        "from_id": rental["from_id"],
        "from_name": rental["from_name"],
        "from_type": rental.get("from_type", 0),
        "to_id": rental["to_id"],
        "to_name": rental["to_name"],
        "rent_end": rental["rent_end"],
        "deck_type": rental["deck_type"],
        "confirm_from": rental["confirm_from"],
        "confirm_to": rental["confirm_to"],
        "main": rental["main"],
        "extra": rental["extra"],
        "side": rental["side"],
    }


def active_rentals(player_id):
    """The rentals not over in which the player lends or borrows."""
    return [
        r
        for r in STATE.rentals
        if rental_active(r) and player_id in (r["from_id"], r["to_id"])
    ]


def push_rentals(player_id):
    """Pushes the player's running rentals (0x2D6C) to their client."""
    STATE.online_push(
        player_id, 0x2D6C, items=[rental_wire(r) for r in active_rentals(player_id)]
    )


def create_rental(lender_id, borrower_id, scene):
    """Starts a rental from a confirmed scene; None if the lender lacks the cards."""
    lender, borrower = STATE.player(lender_id), STATE.player(borrower_id)
    if lender is None or borrower is None:
        return None
    wanted = Counter()
    for part in ("main", "extra", "side"):
        wanted.update(rows_of(scene[part]))
    if not wanted or wanted - tradable_counts(lender_id):
        return None
    end_ts = int(time.time()) + scene["days"] * 86400
    rental = {
        "rental_id": STATE.next_rental_id,
        "from_id": lender_id,
        "from_name": lender["pname"],
        "from_type": 0,
        "to_id": borrower_id,
        "to_name": borrower["pname"],
        "end_ts": end_ts,
        "rent_end": datetime.fromtimestamp(end_ts, JST).strftime("%Y-%m-%d %H:%M:%S"),
        "deck_type": scene["type"],
        "confirm_from": 0,
        "confirm_to": 0,
        "main": scene["main"],
        "extra": scene["extra"],
        "side": scene["side"],
    }
    STATE.next_rental_id += 1
    STATE.rentals.append(rental)
    STATE.save()
    return rental


def tick_rentals(now=None):
    """Ends the rentals whose term is up and tells both sides.

    An ended rental stays in the history with both confirm flags at 1; the
    lender owns the cards again because the client stops subtracting them.
    Returns how many rentals ended.
    """
    now = int(time.time()) if now is None else int(now)
    ended = [r for r in STATE.rentals if rental_active(r) and r["end_ts"] <= now]
    if not ended:
        return 0
    for rental in ended:
        rental["confirm_from"] = rental["confirm_to"] = 1
    STATE.save()
    for player_id in {r["from_id"] for r in ended} | {r["to_id"] for r in ended}:
        push_rentals(player_id)
    return len(ended)


async def rental_ticker():
    """Checks the rental terms once a second (started by main)."""
    while True:
        await asyncio.sleep(1)
        try:
            tick_rentals()
        except Exception:
            log.exception("[Rental] Could not check the rental terms")


# ===========================================================================
# Manner evaluations (dlg_manner.swf)
# ===========================================================================


def request_evaluations(first_id, second_id):
    """After a duel between two players each is asked to rate the other.

    Each gets its own evaluation id (0x2CEC); the answer 0x2CED counts once,
    from the rater, for the player that evaluation names.
    """
    for rater, rated in ((first_id, second_id), (second_id, first_id)):
        eval_id = STATE.next_eval_id
        STATE.next_eval_id += 1
        STATE.evaluations[eval_id] = {"rater": rater, "rated": rated}
        STATE.online_push(rater, 0x2CEC, {"eval_id": eval_id})
    while len(STATE.evaluations) > EVALUATIONS_KEPT:
        del STATE.evaluations[next(iter(STATE.evaluations))]


# ===========================================================================
# User events: Satellite Plaza's tournaments (UserEventScene / lobby)
#
# An event is a plain dict (so it pickles): the form's settings, the host,
# the entrants in entry order and a status of 12 open / 13 running. While it
# runs, event["run"] holds the knockout: the survivors in order, each
# player's round wins (they draw the bracket), the round's duels and timers.
# The server pairs survivors in order, an odd one out goes through on a bye
# (which counts as a win), a duel's winner stays in, and a player who leaves
# forfeits. A finished event leaves the list but its lobby can still ask.
# ===========================================================================


def set_round_delay(seconds):
    """Sets the wait between two rounds (seconds; 0 pairs at once)."""
    global ROUND_DELAY
    ROUND_DELAY = float(seconds)


def uev_run(event):
    """The runtime part of an event, created on first use."""
    return event.setdefault(
        "run",
        {
            "status": 0,  # the lobby's state: 0 open, 4 waiting, 6 paired, 7 duelling, 8 done
            "round": 0,
            "to_go": 0,  # rounds left including this one (names the round)
            "alive": [],
            "wins": {},
            "duels": {},
            "deadline": 0.0,
            "timer": None,
            "winner": 0,
            "second": 0,
            "last_loser": 0,
        },
    )


def uev_of_player(player_id):
    """The open or running event a player is entered in (forfeited ones don't count)."""
    for event in STATE.user_events.values():
        if player_id in event["entrants"] and player_id not in event.get("left", ()):
            return event
    return None


def uev_hosted_by(player_id):
    """The open or running event a player hosts."""
    return next((e for e in STATE.user_events.values() if e["host_id"] == player_id), None)


def uev_lobby_event(event_id):
    """An event the lobby may ask about: live, or finished a moment ago."""
    return STATE.user_events.get(event_id) or STATE.finished_events.get(event_id)


def uev_create_status(player_id, account):
    """0x3400 create_status: bit 4 premium needed, 8 already in an event, 1 full."""
    status = 0
    if not account.premium:
        status |= 4
    if uev_of_player(player_id) is not None or uev_hosted_by(player_id) is not None:
        status |= 8
    if len(STATE.user_events) >= USER_EVENT_MAX_OPEN:
        status |= 1
    return status


def uev_new(host_id, fields):
    """Builds an event from the Create form (0x3390), clamping what the client sent."""

    def number(name, default, low, high):
        return max(low, min(high, as_int(fields.get(name), default)))

    event_id = STATE.next_event_id
    STATE.next_event_id += 1
    event = {
        "event_id": event_id,
        "host_id": host_id,
        "event_name": clip_text(fields.get("event_name", ""), 64) or "Duel Event",
        "comment": clip_text(fields.get("comment", ""), 255),
        "entry_max": number("entry_max", 8, 2, USER_EVENT_MAX_ENTRY),
        "prize_type": number("prize_type", 0, 0, 0xFFFF),
        "level_min": number("level_min", 0, 0, 0xFFFF),
        "level_max": number("level_max", 0, 0, 0xFFFF),
        "eval_level": number("eval_level", 0, 0, 0xFFFF),
        "player_limit": number("player_limit", 0, 0, 0xFFFF),
        "password1": clip_text(fields.get("password1", ""), 32),
        "lang": number("lang", 0, 0, 0xFFFF),
        "match_type": number("match_type", 1, 1, 6),
        "duel_surrender": number("duel_surrender", 1, 0, 1),
        "duel_watch": number("duel_watch", 1, 0, 1),
        "duel_time_turn": number("duel_time_turn", 180, 10, 3600),
        "duel_time_total": number("duel_time_total", 600, 10, 7200),
        "duel_lp_start": number("duel_lp_start", 8000, 100, 99999),
        "regulation_id": number("regulation_id", REGULATION_ID, 0, 0xFFFF) or REGULATION_ID,
        "join_max": number("join_max", 0, 0, 0xFFFF),
        "status": USER_EVENT_OPEN,
        "entrants": [],
        "left": [],
    }
    STATE.user_events[event_id] = event
    STATE.save()
    return event


def uev_entry_error(event, player_id):
    """Why a player cannot enter an event: 0 they can, else an error code."""
    player = STATE.player(player_id)
    if player is None:
        return 700
    if player_id in event["entrants"] and player_id not in event.get("left", ()):
        return 0  # entering again is accepted (e.g. after a restart)
    if event["status"] != USER_EVENT_OPEN:
        return 701
    if len(event["entrants"]) >= event["entry_max"]:
        return 702
    other = uev_of_player(player_id)
    if other is not None and other is not event:
        return 703
    if event["level_max"] and not event["level_min"] <= player["dlv"] <= event["level_max"]:
        return 704
    if event["eval_level"] and not event["eval_level"] >> eval_level(player) & 1:
        return 705
    return 0


def uev_row(event, viewer_id=0):
    """An event as a row of the list 0x177B / the lobby's 0x1780."""
    host = STATE.player(event["host_id"]) or {}
    can_join = uev_entry_error(event, viewer_id) == 0 if viewer_id else True
    return {
        "official": 0,
        "event_id": event["event_id"],
        "event_name": event["event_name"],
        "comment": event["comment"],
        "status": event["status"],
        # Non-zero crosses the row out: the viewer can only close the details.
        "join_flag": 0 if can_join or event["status"] != USER_EVENT_OPEN else 1,
        "password1": event["password1"],
        "prize_type": event["prize_type"],
        "entry_max": event["entry_max"],
        "entry_num": len(event["entrants"]),
        "match_type": event["match_type"],
        "regulation_id": event["regulation_id"],
        "level_min": event["level_min"],
        "level_max": event["level_max"],
        "eval_level": event["eval_level"],
        "lang": event["lang"],
        "player_limit": event["player_limit"],
        "duel_surrender": event["duel_surrender"],
        "duel_watch": event["duel_watch"],
        "duel_time_turn": event["duel_time_turn"],
        "duel_time_total": event["duel_time_total"],
        "duel_lp_start": event["duel_lp_start"],
        "join_max": event["join_max"],
        "player_id": event["host_id"],
        "pname": host.get("pname", ""),
    }


def uev_push(event, request_id, fields=None, items=None, skip=()):
    """Pushes one message to every entrant of the event except `skip`."""
    for player_id in list(event["entrants"]):
        if player_id not in skip:
            STATE.online_push(player_id, request_id, fields, items)


def uev_refresh(event, flags, skip=()):
    """0x32D4: tells the lobbies to ask again (2 entries, 3 bracket, 4 timer, 5 counts, 6 state)."""
    for flag in flags:
        uev_push(event, 0x32D4, {"flag": flag}, skip=skip)


def uev_name(player_id):
    """A player's name, or "" for 0 / an unknown id."""
    player = STATE.player(player_id) if player_id else None
    return player["pname"] if player else ""


def uev_enter(event, player_id):
    """Adds an entrant (or lets a forfeited one back into an open event)."""
    if player_id in event["entrants"]:
        return
    event["entrants"].append(player_id)
    STATE.save()
    uev_push(event, 0x3399, {"entry_name": uev_name(player_id), "entry_type": 1}, skip={player_id})
    uev_refresh(event, (2, 3, 5), skip={player_id})


def uev_leave(event, player_id):
    """An entrant leaves an open event."""
    if player_id not in event["entrants"]:
        return
    event["entrants"].remove(player_id)
    STATE.save()
    uev_push(event, 0x3399, {"entry_name": uev_name(player_id), "entry_type": 2}, skip={player_id})
    uev_refresh(event, (2, 3, 5), skip={player_id})


def uev_cancel(event, skip=()):
    """The host cancels: the others' lobbies close and the event leaves the list."""
    run = event.get("run")
    if run and run.get("timer") is not None:
        run["timer"].cancel()
    uev_push(event, 0x3394, skip=skip)
    STATE.user_events.pop(event["event_id"], None)
    STATE.save()


def uev_round_label(run):
    """The round's name for the lobby: 7 Quarter-Final, 8 Semi-Finals, 9 Final, else the round's number."""
    to_go = run.get("to_go", 0)
    if to_go <= 0:
        return 0
    return 10 - to_go if to_go <= 3 else min(run["round"], 6)


def uev_start(event):
    """The host's Start: everyone is told, then the first round is paired."""
    run = uev_run(event)
    event["status"] = USER_EVENT_RUNNING
    run.update(round=0, alive=list(event["entrants"]), wins={p: 0 for p in event["entrants"]})
    STATE.save()
    uev_push(event, 0x3396)
    uev_begin_round(event)


def uev_begin_round(event):
    """Pairs the survivors in order and starts the round's matches."""
    run = uev_run(event)
    alive = run["alive"]
    if len(alive) < 2:
        uev_finish(event)
        return
    run["round"] += 1
    run["to_go"] = (len(alive) - 1).bit_length()  # rounds left, this one included
    run["timer"], run["deadline"], run["status"] = None, 0.0, 6
    run["duels"] = {}
    for index in range(0, len(alive) - 1, 2):
        duel_id = next_yo3_id("duel")
        run["duels"][duel_id] = {
            "players": (alive[index], alive[index + 1]),
            "winner": None,
            "began": False,
        }
    if len(alive) % 2:
        run["wins"][alive[-1]] = run["wins"].get(alive[-1], 0) + 1  # a bye is a win
    for duel_id, duel in run["duels"].items():
        uev_match_push(event, duel_id, duel)
    banner = {"round": run["round"], "total_round": uev_round_label(run), "status": 1}
    uev_push(event, 0x32D3, banner)
    uev_refresh(event, (6, 5, 2, 3))


def uev_match_push(event, duel_id, duel):
    """0x1978 to each player of a pair: the opponent's record and the event's rule."""
    first, second = duel["players"]
    for me, opponent in ((first, second), (second, first)):
        rival = STATE.player(opponent)
        if rival is None:
            continue
        rule = {
            "name": "duelData",
            "duel_id": duel_id,
            "match_type": event["match_type"],
            "regulation_id": event["regulation_id"],
            "lp_start": event["duel_lp_start"],
            "surrender": event["duel_surrender"],
            "time_turn": event["duel_time_turn"],
            "time_total": event["duel_time_total"],
        }
        versus = dict(make_player_record(rival), name="vsPlayerData")
        STATE.online_push(
            me, 0x1978, items=[MessageRecord(fields=versus), MessageRecord(fields=rule)]
        )


def uev_duel_began(duel_id):
    """A player sat down at a duel's table: the lobbies show "Duel in Progress"."""
    for event in STATE.user_events.values():
        run = event.get("run")
        duel = run["duels"].get(duel_id) if run else None
        if duel is None or duel["began"]:
            continue
        duel["began"] = True
        if run["status"] == 6:
            run["status"] = 7
            uev_refresh(event, (6,))
        return True
    return False


def uev_match_finished(duel_id, winner_id=None):
    """A match's result by its duel id (None = a draw). True if it was an event's."""
    for event in list(STATE.user_events.values()):
        run = event.get("run")
        if run and duel_id in run["duels"]:
            return uev_record_result(event, duel_id, winner_id)
    return False


def uev_report_result(player_id, opponent_id, winner_id):
    """The duel server reports a finished duel by its players: finds the event's match."""
    for event in list(STATE.user_events.values()):
        run = event.get("run")
        for duel_id, duel in (run["duels"].items() if run else ()):
            if duel["winner"] is None and {player_id, opponent_id} == set(duel["players"]):
                return uev_record_result(event, duel_id, winner_id)
    return False


def uev_record_result(event, duel_id, winner_id):
    """Records a duel's winner; the loser is out, and the round may end."""
    run = uev_run(event)
    duel = run["duels"].get(duel_id)
    if duel is None or duel["winner"] is not None:
        return False
    first, second = duel["players"]
    if winner_id not in (first, second):
        winner_id = first  # a draw: the first seat goes on (a guess)
    loser_id = second if winner_id == first else first
    duel["winner"] = winner_id
    run["wins"][winner_id] = run["wins"].get(winner_id, 0) + 1
    if loser_id in run["alive"]:
        run["alive"].remove(loser_id)
    run["last_loser"] = loser_id
    if all(d["winner"] is not None for d in run["duels"].values()):
        uev_round_over(event)
    else:
        uev_refresh(event, (5, 3))
    return True


def uev_round_over(event):
    """Every duel of the round is over: banner, then wait (or finish)."""
    run = uev_run(event)
    uev_push(
        event,
        0x32D3,
        {"round": run["round"], "total_round": uev_round_label(run), "status": 0},
    )
    if len(run["alive"]) <= 1:
        uev_finish(event)
        return
    run["status"] = 4
    uev_refresh(event, (6, 5, 2, 3))
    if ROUND_DELAY > 0:
        # The winners are still on their result screens: a match push that
        # arrives there is lost, so the lobby waits and counts down.
        run["deadline"] = time.time() + ROUND_DELAY
        uev_push(event, 0x3301, {"timer": int(ROUND_DELAY)})
        run["timer"] = asyncio.get_running_loop().call_later(
            ROUND_DELAY, uev_next_round, event["event_id"]
        )
    else:
        uev_next_round(event["event_id"])


def uev_next_round(event_id):
    """The wait is over: the countdown clears and the next round is paired."""
    event = STATE.user_events.get(event_id)
    if event is None or event["status"] != USER_EVENT_RUNNING:
        return
    run = uev_run(event)
    run["timer"] = None
    if len(run["alive"]) < 2:
        uev_finish(event)
        return
    uev_push(event, 0x3302)
    uev_begin_round(event)


def uev_finish(event):
    """The event is over: winner and runner-up are announced, the event leaves the list."""
    run = uev_run(event)
    if run.get("timer") is not None:
        run["timer"].cancel()
        run["timer"] = None
    winner = run["alive"][0] if run["alive"] else 0
    second = run["last_loser"] if winner else 0
    run.update(status=8, winner=winner, second=second, deadline=0.0)
    event["status"] = USER_EVENT_DONE
    uev_push(
        event,
        0x3307,
        {
            "winner": winner,
            "winner_name": uev_name(winner),
            "second": second,
            "second_name": uev_name(second),
        },
    )
    uev_refresh(event, (6, 5, 2, 3))
    STATE.user_events.pop(event["event_id"], None)
    STATE.finished_events[event["event_id"]] = event
    while len(STATE.finished_events) > USER_EVENTS_FINISHED_KEPT:
        del STATE.finished_events[next(iter(STATE.finished_events))]
    STATE.save()


def uev_player_left(player_id):
    """A player disconnected or left: out of an open event, a forfeit in a running one."""
    event = uev_of_player(player_id)
    if event is None:
        return
    if event["status"] == USER_EVENT_OPEN:
        if player_id == event["host_id"]:
            uev_cancel(event, skip={player_id})
        else:
            uev_leave(event, player_id)
        return
    run = uev_run(event)
    event.setdefault("left", []).append(player_id)
    duel_id = next(
        (
            d_id
            for d_id, d in run["duels"].items()
            if d["winner"] is None and player_id in d["players"]
        ),
        None,
    )
    if duel_id is not None:
        first, second = run["duels"][duel_id]["players"]
        uev_record_result(event, duel_id, second if first == player_id else first)
    elif player_id in run["alive"]:
        run["alive"].remove(player_id)
        run["last_loser"] = player_id
        if len(run["alive"]) <= 1 and run["status"] in (4, 6, 7):
            if all(d["winner"] is not None for d in run["duels"].values()):
                uev_finish(event)


# --- The lobby's views of an event ------------------------------------------


def uev_state_wire(event):
    """0x32FB: the lobby's state, round name and (once over) the podium."""
    run = uev_run(event)
    return {
        "event_id": event["event_id"],
        "status": run["status"],
        "phase": 0,
        "round": run["round"],
        "total_round": uev_round_label(run),
        "winner": run["winner"],
        "winner_name": uev_name(run["winner"]),
        "second": run["second"],
        "second_name": uev_name(run["second"]),
        "tournament_id": 0,
        "championship_id": 0,
        "year": 0,
        "season": 0,
    }


def uev_block_exists(event, block):
    """Blocks 0-0x3F hold 8 seats each, 0x40-0x47 the champions of eight of those, 0x48 theirs."""
    seats = len(event["entrants"])
    if block < 0x40:
        return block * 8 < seats
    if block < 0x48:
        return ((block - 0x40) * 8 + 1) * 8 < seats  # needs the group's second block
    return block == 0x48 and 72 < seats


def uev_map_data(event):
    """0x3309: one flag byte per bracket block (0x49 of them)."""
    return bytes(1 if uev_block_exists(event, b) else 0 for b in range(0x49))


def uev_champion(event, level, index):
    """The player who won block `index` of a level (3 wins per level), else 0."""
    wins = uev_run(event)["wins"]
    return next(
        (p for p in uev_seats(event, level, index) if p and wins.get(p, 0) >= 3 * (level + 1)),
        0,
    )


def uev_seats(event, level, index):
    """The eight seats of a block: entrants (level 0) or lower blocks' champions."""
    if level == 0:
        entrants = event["entrants"]
        return [
            entrants[index * 8 + k] if index * 8 + k < len(entrants) else 0 for k in range(8)
        ]
    return [uev_champion(event, level - 1, index * 8 + k) for k in range(8)]


def uev_block_item(event, block):
    """0x32FF's item for a bracket block, None if the block doesn't exist."""
    if not uev_block_exists(event, block):
        return None
    level, index = (0, block) if block < 0x40 else (1, block - 0x40) if block < 0x48 else (2, 0)
    wins = uev_run(event)["wins"]
    item = {"block_number": block, "start_level": level}
    for number, player_id in enumerate(uev_seats(event, level, index), 1):
        item[f"player_id{number}"] = player_id
        item[f"win_count{number}"] = (
            max(0, min(3, wins.get(player_id, 0) - 3 * level)) if player_id else 0
        )
    return item


def uev_phase_blocks(event):
    """The blocks the round's unfinished duels play in (0x3305): up to three, -1 for none."""
    run = uev_run(event)
    entrants = event["entrants"]
    blocks = []
    for duel in run["duels"].values():
        if duel["winner"] is not None:
            continue
        first, second = duel["players"]
        level = min(run["wins"].get(first, 0), run["wins"].get(second, 0)) // 3
        base = entrants.index(first) // 8 if first in entrants else 0
        block = base if level == 0 else 0x40 + base // 8 if level == 1 else 0x48
        if block not in blocks:
            blocks.append(block)
    return (blocks + [0xFFFFFFFF] * 3)[:3]


def player_left(player_id):
    """Everything a player's departure ends: invitations, scenes, an event, a match wait.

    Called when their connection drops, when they pick another character
    and when their character is deleted.
    """
    drop_invites(player_id)
    scene = STATE.scenes.get(player_id)
    if scene is not None:
        scene_end(scene, notify=scene_partner(scene, player_id))
    uev_player_left(player_id)
    if FRONT_MATCHMAKER is not None:
        FRONT_MATCHMAKER.remove_player(player_id)


# One YO3 card row: row, cid, name and stats.
@dataclass(frozen=True)
class Card:
    row: int
    cid: int
    atk: int
    defense: int
    level: int
    attribute: int
    race: int
    name: str


# A built-in deck (starter, structure, NPC): main/extra/side rows.
@dataclass
class PresetDeck:
    deck_id: int
    kind: int
    name_id: int
    main: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    side: list = field(default_factory=list)

    # Every row of the deck in one list.
    def cards(self):
        return self.main + self.extra + self.side


# A banlist: lists[n] holds the cids limited to n copies (default 3); some cards are never allowed.
@dataclass
class Regulation:
    regulation_id: int
    lists: list
    default_limit: int = 3

    # How many copies of a card the regulation allows.
    def limit(self, cid):
        for index in range(4):
            if index != self.default_limit and cid in self.lists[index]:
                return index
        return self.default_limit

    # True if no card exceeds its limit and none is on the never-allowed list.
    def allows(self, cids):
        return all(
            cid not in NEVER_ALLOWED_CARDS and count <= self.limit(cid)
            for cid, count in Counter(cids).items()
        )


# The game's data files are missing or malformed.
class GameDataError(Exception):
    pass


# Coerces ints, bools and numeric text (the client sends some ids as text) to int, else default.
def as_int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


# Decodes a binary field of big-endian u16 values into a list.
def u16_list(fields, name):
    raw = fields.get(name)
    if not isinstance(raw, bytes):
        return []
    count = len(raw) // 2
    return list(struct.unpack(f">{count}H", raw[: count * 2]))


# Encodes rows as big-endian u16 bytes.
def pack_rows(rows):
    return struct.pack(f">{len(rows)}H", *rows)


# Reads a NUL-terminated UTF-16 string from a buffer.
def utf16z(buffer, start):
    if start >= len(buffer):
        return ""
    end = buffer.find(b"\x00\x00", start)
    while end != -1 and (end - start) % 2:
        end = buffer.find(b"\x00\x00", end + 1)
    if end == -1:
        end = len(buffer) - (len(buffer) - start) % 2
    return buffer[start:end].decode("utf-16le", errors="replace")


# Reader for the game's .ypk archives.
class Ypk:
    SIGNATURE = b"YO\x03\x04"

    # Indexes the archive's entries.
    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}

        try:
            with self.path.open("rb") as file:
                size = os.fstat(file.fileno()).st_size
                position = 0

                while position + 30 <= size:
                    file.seek(position)
                    head = file.read(30)

                    if len(head) < 30 or head[:4] != self.SIGNATURE:
                        break

                    method = struct.unpack_from("<H", head, 8)[0]
                    compressed, original = struct.unpack_from("<II", head, 18)
                    name_length, extra_length = struct.unpack_from("<HH", head, 26)

                    name_bytes = file.read(name_length)

                    if len(name_bytes) != name_length:
                        raise GameDataError(f"Truncated filename in {self.path}")

                    name = name_bytes.decode("utf-8", errors="replace")

                    offset = position + 30 + name_length + extra_length

                    if offset + compressed > size:
                        raise GameDataError(f"Invalid archive entry: {name}")

                    self.entries.setdefault(
                        name, (method, compressed, original, offset)
                    )

                    position = offset + compressed

        except OSError as exc:
            raise GameDataError(f"Cannot read {self.path}: {exc}") from exc

        log.info("[Ypk] Loaded %s entries from %s", len(self.entries), self.path.name)

    # Contents of an entry, or None.
    def read(self, name):
        entry = self.entries.get(name)

        if entry is None:
            return None

        method, compressed, original, offset = entry

        with self.path.open("rb") as file:
            file.seek(offset)
            raw = file.read(compressed)

        if len(raw) != compressed:
            raise GameDataError(f"{name} is truncated")

        if method == 0:
            data = raw
        elif method == 8:
            data = zlib.decompress(raw, -15)
        else:
            raise GameDataError(f"Unsupported compression method {method}: {name}")

        if len(data) != original:
            log.warning(
                "[Ypk] Size mismatch for %s: expected %s, got %s",
                name,
                original,
                len(data),
            )

        return data

    # Contents of an entry; GameDataError if missing.
    def read_required(self, name):
        data = self.read(name)

        if data is None:
            raise GameDataError(f"{name} is missing from {self.path.name}")

        return data

    # Entries of a directory with the given extensions.
    def records(self, directory, extensions):
        directory = directory.replace("\\", "/").strip("/").lower() + "/"

        extensions = tuple(ext.lower() for ext in extensions)

        for name in sorted(self.entries):
            normalized = name.replace("\\", "/").lower()

            if not normalized.startswith(directory):
                continue

            if not normalized.endswith(extensions):
                continue

            try:
                data = self.read_required(name)
                fields = decode_data_file(data)

                yield name, fields

            except (ValueError, struct.error, zlib.error, GameDataError) as exc:
                log.warning("[Ypk] Skipping unreadable %s: %s", name, exc)


# Decodes (decrypts/decompresses) one of the game's data files.
def decode_data_file(blob):
    if len(blob) < 4:
        raise ValueError("data file too short")
    length = struct.unpack(">I", blob[:4])[0] ^ LENGTH_MASK
    body = blob[4 : 4 + length]
    if len(body) != length:
        raise ValueError("data file truncated")
    message, _ = decode_message(decrypt_body(body))
    return message["fields"]


# Parses the CARD_Prop table into Card objects.
def parse_card_table(prop, index, names):
    cards = {}
    for row in range(len(prop) // 8):
        a, b = struct.unpack_from("<II", prop, row * 8)
        cid = a & 0x3FFF
        if cid == 0:
            continue
        name = ""
        if row * 8 + 4 <= len(index):
            offset = struct.unpack_from("<I", index, row * 8)[0]
            name = utf16z(names, offset)

        cards[row] = Card(
            row=row,
            cid=cid,
            atk=((a >> 14) & 0x1FF) * 10,
            defense=((a >> 23) & 0x1FF) * 10,
            level=(b >> 12) & 0xF,
            attribute=(b >> 8) & 0xF,
            race=(b >> 19) & 0x1F,
            name=name,
        )
    return cards


# Parses a text table into {id: string}.
def parse_text_table(table):
    texts = {}
    if len(table) < 4:
        return texts
    count = struct.unpack_from("<I", table, 0)[0]
    base = 8 + 8 * count
    for i in range(count):
        at = 4 + 8 * i
        if at + 8 > len(table):
            break
        text_id, offset = struct.unpack_from("<II", table, at)
        if base + offset > len(table):
            continue
        texts[text_id] = utf16z(table, base + offset)
    return texts


# Everything read from the game folder: cards, packs, decks, regulations, NPCs, items.
class GameData:
    # Starts empty (loaded is False).
    def __init__(self):
        self.cards = {}
        self.regulations = []
        self.decks = []
        self.texts = {}
        self.packs = []
        self.structure_decks = []
        self.protectors = []
        self.avatar_items = []
        self.protector_items = []
        self.starter_deck = None
        self.official_decks = []

    # True once the card table is present.
    @property
    def loaded(self):
        return bool(self.cards)

    # Highest card row: the length of a trunk's `cards` array.
    @property
    def card_rows(self):
        return max(self.cards, default=FALLBACK_CARD_ROWS)

    # Reads all the game's archives and builds the tables.
    @classmethod
    def load(cls, game_dir):
        archive = Path(game_dir) / "package" / "data002.ypk"
        if not archive.is_file():
            raise GameDataError(
                f"{archive} not found. Point YO3_GAME_DIR (or --game-dir) at the installation folder."
            )

        ypk = Ypk(archive)
        data = cls()
        data.cards = parse_card_table(
            ypk.read_required("data/bin#/CARD_Prop.bin"),
            ypk.read_required("data/bin#/CARD_Indx_E.bin"),
            ypk.read_required("data/bin#/CARD_Name_E.bin"),
        )
        for _, fields in ypk.records("data/regulation/", (".y3r",)):
            data.regulations.append(
                Regulation(
                    regulation_id=as_int(fields.get("regulation_id")),
                    lists=[
                        u16_list(fields, key)
                        for key in ("card0", "card1", "card2", "card3")
                    ],
                    default_limit=as_int(fields.get("addition_card"), 3),
                )
            )
        data.regulations.sort(key=lambda r: r.regulation_id)

        for _, fields in ypk.records("data/recipe/", (".y3d", ".y3a")):
            deck = PresetDeck(
                deck_id=as_int(fields.get("deck_id")),
                kind=as_int(fields.get("deck_type")),
                name_id=as_int(fields.get("name_id")),
                main=u16_list(fields, "main"),
                extra=u16_list(fields, "extra"),
                side=u16_list(fields, "side"),
            )
            data.decks.append(deck)
            if deck.kind == 2:
                data.structure_decks.append(ShopDeck(deck=deck))

        kind_counts = {}
        for deck in data.decks:
            kind_counts[deck.kind] = kind_counts.get(deck.kind, 0) + 1

        target_name_id = NPC_DECK_TITLE + 2 * 135
        print("[DEBUG] Recipe deck counts by kind:", kind_counts)
        print("[DEBUG] NPC 135 expected name_id:", target_name_id)

        for deck in data.decks:
            if abs(deck.name_id - target_name_id) <= 20:
                print(
                    "[DEBUG] Nearby recipe:",
                    "deck_id=",
                    deck.deck_id,
                    "kind=",
                    deck.kind,
                    "name_id=",
                    deck.name_id,
                    "main_cards=",
                    len(deck.main),
                )

        pack_paths = [
            p
            for p in ypk.entries
            if any(folder in str(p).lower() for folder in ("data", "package"))
            and str(p).lower().endswith((".y3a", ".y3p", ".y3d"))
        ]

        if not pack_paths:
            pack_paths = [
                p
                for p in ypk.entries
                if str(p).lower().endswith((".y3a", ".y3p", ".y3d"))
            ]

        for path in pack_paths:
            try:
                raw_data = ypk.read_required(path)
                fields = decode_data_file(raw_data)
            except Exception:
                continue

            if not fields:
                continue

            pack_id = as_int(fields.get("pack_id") or fields.get("id"))
            cards = u16_list(fields, "pack") or u16_list(fields, "cards")

            if pack_id <= 0 or not cards:
                continue

            if any(p.id == pack_id for p in data.packs):
                continue

            data.packs.append(
                ShopPack(
                    id=pack_id,
                    tiers=split_tiers(cards),
                    released=fields.get("created", ITEM_DATE),
                    modified=fields.get("modified", ITEM_DATE),
                    pack_type=as_int(fields.get("pack_type"), 1),
                    image=fields.get("pack_image", "noImage"),
                    name_id=as_int(fields.get("name_id")),
                    description_id=as_int(fields.get("discription_id")),
                )
            )

        log.info(
            "[GameData] Successfully loaded %d total booster packs from data and package folders",
            len(data.packs),
        )

        data.decks.sort(key=lambda d: d.deck_id)
        data.texts = parse_text_table(ypk.read_required("data/text/txttbl_E.bin"))
        data.starter_deck = data.find_starter_deck(STARTER_REGULATION)

        for npc_id, duelist in OFFICIAL_DECKS:
            deck = data.find_npc_deck(npc_id)
            if deck is None:
                continue
            title = data.texts.get(NPC_DECK_TITLE + 2 * npc_id, "")
            data.official_decks.append((f"{duelist} - {title}", deck))

        return data

    # The starter deck for a regulation.
    def find_starter_deck(self, regulation_id):
        regulation = next(
            (r for r in self.regulations if r.regulation_id == regulation_id), None
        )
        if regulation is None:
            return None
        for deck in self.decks:
            if deck.kind != 1:
                continue
            cids = [self.cards[row].cid for row in deck.cards() if row in self.cards]
            if regulation.allows(cids):
                return deck
        return None

    # The deck an NPC plays.
    def find_npc_deck(self, npc_id):
        name_id = NPC_DECK_TITLE + 2 * npc_id
        return next(
            (d for d in self.decks if d.kind == 3 and d.name_id == name_id), None
        )

    # Cards whose name contains the text (case-insensitive), up to `limit`.
    def search(self, text, limit=30):
        text = text.strip().lower()
        found = [
            card
            for card in self.cards.values()
            if text in card.name.lower() or text == str(card.cid)
        ]
        found.sort(key=lambda card: (card.name, card.row))
        return found[:limit]


GAME = GameData()
DUEL_ENGINE = None
DUEL_MANAGER = None
FRONT_MATCHMAKER = None

DUEL_CONNECTIONS = {}
YO3_JOIN_COUNTER = int(time.time()) & 0x7FFFFFFF
YO3_DUEL_COUNTER = int(time.time() * 1000) & 0x7FFFFFFF


# Next join id or duel id (31-bit counters).
def next_yo3_id(which="join"):
    global YO3_JOIN_COUNTER, YO3_DUEL_COUNTER
    if which == "duel":
        YO3_DUEL_COUNTER = (YO3_DUEL_COUNTER + 1) & 0x7FFFFFFF
        return YO3_DUEL_COUNTER
    YO3_JOIN_COUNTER = (YO3_JOIN_COUNTER + 1) & 0x7FFFFFFF
    return YO3_JOIN_COUNTER


# Loads the game data and builds the shop catalogues (protectors, sleeves, disks, avatar items).
def init_game_data():
    global GAME

    try:
        GAME = GameData.load(GAME_DIR)

    except (GameDataError, zlib.error) as exc:
        GAME = GameData()
        log.error(
            "Could not read the game data: %s",
            exc,
        )

    def make_shop_item(item_id, item_type, cost):
        return type(
            "Item",
            (),
            {
                "item_type": item_type,
                "item_id": item_id,
                "cost": cost,
            },
        )()

    catalogue_ypk = Ypk(Path(GAME_DIR) / "package" / "data002.ypk")

    protector_ids = sorted(
        {
            int(filename.rsplit("/", 1)[-1][1:-4])
            for filename in map(str, catalogue_ypk.entries)
            if filename.lower().startswith("data/protector/p")
            and filename.lower().endswith(".png")
            and filename.rsplit("/", 1)[-1][1:-4].isdigit()
            and int(filename.rsplit("/", 1)[-1][1:-4]) > 0
        }
    )

    sleeve_ids = sorted(
        {
            int(filename.rsplit("/", 1)[-1][1:-4])
            for filename in map(str, catalogue_ypk.entries)
            if filename.lower().startswith("data/protector/s")
            and filename.lower().endswith(".png")
            and filename.rsplit("/", 1)[-1][1:-4].isdigit()
            and int(filename.rsplit("/", 1)[-1][1:-4]) > 0
        }
    )

    duel_disk_ids = sorted(
        {
            int(filename.rsplit("/", 1)[-1][2:-4])
            for filename in map(str, catalogue_ypk.entries)
            if "dd" in filename.lower()
            and filename.lower().endswith(".png")
            and filename.rsplit("/", 1)[-1].startswith("dd")
            and filename.rsplit("/", 1)[-1][2:-4].isdigit()
            and int(filename.rsplit("/", 1)[-1][2:-4]) > 0
        }
    )

    GAME.protector_items = [
        make_shop_item(item_id, PROTECTOR_TYPE, PROTECTOR_COST)
        for item_id in protector_ids
    ]

    GAME.sleeve_items = [
        make_shop_item(item_id, PROTECTOR_TYPE, PROTECTOR_COST)
        for item_id in sleeve_ids
    ]

    if duel_disk_ids:
        GAME.duel_disk_items = [
            make_shop_item(item_id, PROTECTOR_TYPE, PROTECTOR_COST)
            for item_id in duel_disk_ids
        ]
    else:
        log.warning(
            "[GameData] No real duel disk images found in archive; using fallback."
        )
        GAME.duel_disk_items = [
            make_shop_item(i, PROTECTOR_TYPE, PROTECTOR_COST) for i in range(1, 61)
        ]

    if not getattr(GAME, "avatar_items", None):
        log.warning(
            "[GameData] No real avatar catalogue loaded; "
            "using temporary placeholder items."
        )

        GAME.avatar_items = [
            make_shop_item(i, AVATAR_ITEM_TYPE, AVATAR_ITEM_COST) for i in range(1, 150)
        ]

    log.info("[GameData] Protectors: %d", len(GAME.protector_items))
    log.info("[GameData] Card sleeves: %d", len(GAME.sleeve_items))
    log.info("[GameData] Duel disks: %d", len(GAME.duel_disk_items))
    log.info("[GameData] Avatar items: %d", len(GAME.avatar_items))
    log.info("[GameData] Booster packs: %d", len(GAME.packs))
    log.info("[GameData] Structure decks: %d", len(GAME.structure_decks))

    return GAME


# One-line summary of what was loaded.
def describe_game_data():
    if not GAME.loaded:
        return "NOT LOADED"
    starter = f"deck {GAME.starter_deck.deck_id}" if GAME.starter_deck else "none found"
    return (
        f"{len(GAME.cards)} cards (trunk of {GAME.card_rows} bytes), "
        f"{len(GAME.regulations)} regulations, "
        f"{len(GAME.decks)} preset decks, starter: {starter}, "
        f"official decks: {len(GAME.official_decks)}, "
        f"packs: {len(GAME.packs)}, shop structure decks: {len(GAME.structure_decks)}"
    )


# Names of the built-in decks.
def preset_names():
    names = {STARTER_DECK_NAME}
    names.update(name for name, _ in GAME.official_decks)
    return names


# A preset deck as a saved Deck.
def preset_as_deck(preset, deck_id, name):
    return Deck(
        deck_id=deck_id,
        deck_name=name[:MAX_DECK_NAME],
        main=pack_rows(preset.main),
        extra=pack_rows(preset.extra),
        side=pack_rows(preset.side),
    )


# A preset deck as {main, extra, side} bytes for the duel manager.
def preset_payload(preset):
    return {
        "main": pack_rows(preset.main),
        "extra": pack_rows(preset.extra),
        "side": pack_rows(preset.side),
    }


# {row: count} a player owns: grants + bought/got cards, less the ones given away.
def player_collection(player):
    owned = Counter()
    if GRANT_STARTER_DECK and GAME.starter_deck is not None:
        owned.update(GAME.starter_deck.cards())
    if GRANT_OFFICIAL_DECKS:
        for _, deck in GAME.official_decks:
            owned.update(deck.cards())
    for row, count in player.get("cards", {}).items():
        owned[row] += count
    for row, count in player.get("given", {}).items():
        owned[row] = max(0, owned[row] - count)
    return {row: n for row, n in owned.items() if n > 0}


# The trunk's `cards` and `noTrade` byte arrays (one byte per row).
def build_trunk(player):
    rows = GAME.card_rows
    cards = bytearray(rows)
    for row, count in player_collection(player).items():
        if 1 <= row <= rows:
            cards[row - 1] = min(count, 255)
    return bytes(cards), bytes(rows)


# Gives a player copies of a card (CLI --grant-card).
def grant_card(player_id, row, quantity=1):
    row, quantity = int(row), int(quantity)
    if quantity < 1:
        raise ValueError("Quantity must be at least 1")
    if row not in GAME.cards:
        raise ValueError(f"Unknown card row: {row}")
    player = STATE.player(player_id)
    if player is None:
        STATE.get_account(USER_ID)
        player = STATE.player(player_id)
    if player is None:
        raise ValueError(f"Player {player_id} does not exist")
    cards = player.setdefault("cards", {})
    cards[row] = cards.get(row, 0) + quantity
    STATE.save()


# (card, count) pairs a player owns (CLI --inventory).
def get_inventory(player_id):
    player = STATE.player(player_id)
    if player is None:
        STATE.get_account(USER_ID)
        player = STATE.player(player_id)
    if player is None:
        raise ValueError(f"Player {player_id} does not exist")
    return [
        (GAME.cards[row], count)
        for row, count in sorted(player_collection(player).items())
        if row in GAME.cards
    ]


# Reverses the byte order inside every 32-bit word (the client's Blowfish layout).
def swap_u32_words(data):
    if len(data) % 4:
        raise ValueError("Data is not aligned to 32-bit words")
    return b"".join(data[i : i + 4][::-1] for i in range(0, len(data), 4))


# Blowfish-encrypts a message body in the client's word order.
def encrypt_body(data):
    data = bytes(data)
    padding = (-len(data)) % 8
    data += b"\x00" * padding
    transformed = swap_u32_words(data)
    cipher = Blowfish.new(KEY, Blowfish.MODE_ECB)
    encrypted = cipher.encrypt(transformed)
    return swap_u32_words(encrypted)


# Inverse of encrypt_body.
def decrypt_body(data):
    if not data or len(data) % 8:
        raise ValueError("Invalid Blowfish data length")
    transformed = swap_u32_words(data)
    cipher = Blowfish.new(KEY, Blowfish.MODE_ECB)
    decrypted = cipher.decrypt(transformed)
    return swap_u32_words(decrypted)


# Encrypts a message and prefixes the masked length.
def encode_frame(message):
    encrypted = encrypt_body(message)
    if len(encrypted) > MAX_PACKET_SIZE:
        raise ValueError("Outgoing packet too large")
    encoded_length = len(encrypted) ^ LENGTH_MASK
    return struct.pack(">I", encoded_length) + encrypted


# Reads and decrypts one frame from a stream.
async def read_frame(reader):
    header = await reader.readexactly(4)
    encoded_length = struct.unpack(">I", header)[0]
    packet_length = encoded_length ^ LENGTH_MASK
    if packet_length <= 0 or packet_length > MAX_PACKET_SIZE or packet_length % 8:
        raise ValueError(f"Invalid packet format: {packet_length}")
    encrypted = await reader.readexactly(packet_length)
    return decrypt_body(encrypted)


TYPE_UINT32 = 5
TYPE_TEXT = 7
TYPE_BINARY = 8


# Encodes one field value: int -> uint32, str -> text, bytes -> binary.
def encode_value(value):
    if isinstance(value, bool):
        return (TYPE_UINT32, struct.pack(">I", int(value)))
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("Integer outside uint32 range")
        return (TYPE_UINT32, struct.pack(">I", value))
    if isinstance(value, str):
        raw = value.encode("utf-8") + b"\x00"
        return (TYPE_TEXT, struct.pack(">I", len(raw)) + raw)
    if isinstance(value, bytes):
        return (TYPE_BINARY, struct.pack(">I", len(value)) + value)
    raise TypeError(f"Unsupported YO3 value type: {type(value)}")


# Accepts a dict or MessageRecord and returns (fields, items, tag).
def normalize_record(item):
    if isinstance(item, MessageRecord):
        return item.fields, item.items, item.tag
    if isinstance(item, dict):
        return item, [], TAG_RECORD
    raise TypeError(f"Invalid YO3 record: {type(item)}")


# Serialises fields and nested items into the wire format.
def encode_message(fields=None, items=None, tag=4, depth=0):
    if depth > MAX_NESTING:
        raise ValueError("Message nesting too deep")
    fields = fields or {}
    items = items or []
    output = bytearray()
    output.append(tag)
    output.extend(struct.pack(">H", len(fields)))

    for name, value in fields.items():
        name_bytes = name.encode("ascii")
        if not name_bytes or len(name_bytes) > 255:
            raise ValueError(f"Invalid field name: {name}")
        value_type, encoded_value = encode_value(value)
        output.append(len(name_bytes))
        output.extend(name_bytes)
        output.append(value_type)
        output.extend(encoded_value)

    output.extend(struct.pack(">H", len(items)))
    for item in items:
        item_fields, item_children, item_tag = normalize_record(item)
        item_bytes = encode_message(
            fields=item_fields, items=item_children, tag=item_tag, depth=depth + 1
        )
        output.extend(item_bytes)
    return bytes(output)


# A message whose `request` field is the given id.
def make_reply(request_id, fields=None, items=None):
    reply_fields = {"request": request_id}
    if fields:
        reply_fields.update(fields)
    return encode_message(fields=reply_fields, items=items, tag=4)


# Parses the wire format into {tag, fields, items} (bounded nesting).
def decode_message(data, offset=0, depth=0):
    if depth > MAX_NESTING:
        raise ValueError("Maximum nesting exceeded")
    if offset + 3 > len(data):
        raise ValueError("Incomplete message header")

    tag = data[offset]
    offset += 1
    field_count = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    fields = {}

    for _ in range(field_count):
        if offset >= len(data):
            raise ValueError("Incomplete field name")
        name_length = data[offset]
        offset += 1
        if offset + name_length + 1 > len(data):
            raise ValueError("Incomplete field")
        name = data[offset : offset + name_length].decode("ascii", errors="replace")
        offset += name_length
        value_type = data[offset]
        offset += 1

        if value_type in (5, 6):
            if offset + 4 > len(data):
                raise ValueError("Incomplete integer")
            value = struct.unpack_from(">I", data, offset)[0]
            offset += 4
        elif value_type in (7, 8, 9):
            if offset + 4 > len(data):
                raise ValueError("Incomplete value length")
            length = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            if length > MAX_PACKET_SIZE or offset + length > len(data):
                raise ValueError("Incomplete field value")
            raw = data[offset : offset + length]
            offset += length
            value = (
                raw
                if value_type == 8
                else raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            )
        else:
            raise ValueError(f"Unknown value type: {value_type}")

        fields[name] = value

    if offset + 2 > len(data):
        raise ValueError("Missing item count")
    item_count = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    items = []
    for _ in range(item_count):
        item, offset = decode_message(data, offset, depth + 1)
        items.append(item)

    return {"tag": tag, "fields": fields, "items": items}, offset


# Truncates text to a UTF-8 byte limit without splitting a character.
def clip_text(value, maximum):
    if not isinstance(value, str):
        return ""
    raw = value.encode("utf-8")[:maximum]
    while True:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]


# The fields of a decoded record.
def record_fields(record):
    if isinstance(record, dict):
        return record.get("fields", record)
    return {}


# One front/gate connection: reads requests, answers them and receives pushes for its player.
class ClientSession:
    # Binds the stream and the (single) account; no player selected yet.
    def __init__(self, reader, writer, service):
        self.reader = reader
        self.writer = writer
        self.service = service
        self.addr = writer.get_extra_info("peername")

        self.account = STATE.get_account(USER_ID)
        self.selected_player = None
        self.closed = False
        self.send_lock = asyncio.Lock()

        self.lang = 1
        self.country = "GB"
        self.room_id = None

        self.npc_join_id = None
        self.npc_event_id = None
        self.npc_duel_id = None
        self.npc_id = None
        self.npc_native_duel_id = None

    # Writes bytes under the send lock so frames never interleave.
    async def send_raw(self, data):
        async with self.send_lock:
            if self.closed:
                return

            self.writer.write(data)
            await self.writer.drain()

    # Encodes and sends one message.
    async def send_message(self, request_id, fields=None, items=None):
        message = make_reply(request_id, fields, items)

        if request_id == 0x23F0:
            log.info(
                "[%s] PREMIUM OUTGOING 0x23F0 fields=%r items=%r",
                self.service,
                fields,
                items,
            )

        await self.send_raw(encode_frame(message))

        log.info(
            "[%s] Sent 0x%04X",
            self.service,
            request_id,
        )

    # Sends a server-initiated message (same wire format as a reply).
    async def send_push(self, request_id, fields=None, items=None):
        await self.send_message(
            request_id,
            fields=fields,
            items=items,
        )

    # The selected player character, or None.
    def player(self):
        if self.selected_player is None:
            return None

        return self.account.find_player(self.selected_player)

    # Sends the collection (9000) plus the friend, ignore and rental lists the client expects with
    # it.
    async def trunk(self):
        player = self.player()

        if player is None:
            log.warning(
                "Cannot send trunk: no selected player for session %s",
                self.addr,
            )
            return

        if not GAME.loaded:
            log.warning("Game data is not loaded; trunk may be incomplete")

        cards, no_trade = build_trunk(player)

        await self.send_message(
            0x2328,
            items=[
                {
                    "cards": cards,
                    "noTrade": no_trade,
                }
            ],
        )

        await self.send_push(0x251C, items=friend_list_items(self.selected_player))
        await self.send_push(0x252A, items=ignore_list_items(self.selected_player))
        push_rentals(self.selected_player)

        log.info(
            "Trunk sent for player %s (%d card types owned)",
            self.selected_player,
            sum(1 for count in cards if count),
        )

    # 0x2588: how many mails wait.
    async def mail_count(self):
        player = self.player()

        if player is None:
            log.warning("Cannot send mail count: no selected player")
            return

        count = len(STATE.mailboxes.get(self.selected_player, []))

        await self.send_message(0x2588, fields={"count": count})

        log.info("Mail count sent for player %s: %d", self.selected_player, count)

    # 0x23F0: the Duel Points balance and premium status.
    async def send_bp_balance(self):
        bp = max(
            0,
            int(getattr(self.account, "duel_points", 0) or 0),
        )

        log.info(
            "[Front] Current account Booster Pass balance: %d",
            bp,
        )

        await self.send_message(
            0x23F0,
            fields={
                "dpauth_result": 0,
                "dpPoints": bp,
                "dpGetPoints": 0,
                "premium": getattr(self.account, "premium", 0),
                "premium_expire": PREMIUM_EXPIRE,
            },
        )

    # 0x1C22: the booster-pack shelf.
    async def send_pack_shelf(self):
        packs = getattr(GAME, "packs", []) or []

        # The archive contains several pack_type values across releases.
        # Send the complete loaded catalogue so special packs are not silently
        # dropped when their type uses a value other than 1 or 4.
        normal_packs = packs
        special_packs = []

        log.info(
            "[Shop] Normal packs: %d | Special packs: %d",
            len(normal_packs),
            len(special_packs),
        )

        await self.send_message(
            0x1C22,
            items=[shelf_item(pack) for pack in normal_packs + special_packs],
        )

    # Avatar item shelf.
    async def send_avatar_shelf(self):
        avatar_items = getattr(GAME, "avatar_items", []) or []

        log.info(
            "[Shop] Avatar items available: %d",
            len(avatar_items),
        )

        if not avatar_items:
            log.error(
                "[Shop] Avatar catalogue is empty. "
                "GAME.avatar_items must be loaded from game data."
            )
            return

        await self.send_message(
            0x1C2A,
            items=[
                shelf_entry(
                    item.item_type,
                    item.item_id,
                    item.cost,
                )
                for item in avatar_items
            ],
        )

    # Card protector shelf.
    async def send_protector_shelf(self):
        protector_items = (
            getattr(GAME, "protector_items", [])
            or []
        ) + (
            getattr(GAME, "sleeve_items", [])
            or []
        ) + (
            getattr(GAME, "duel_disk_items", [])
            or []
        )

        log.info(
            "[Shop] Protector items available: %d",
            len(protector_items),
        )

        if not protector_items:
            log.error(
                "[Shop] Protector catalogue is empty. "
                "GAME.protector_items must be loaded from game data."
            )
            return

        await self.send_message(
            0x1C32,
            items=[
                shelf_entry(
                    item.item_type,
                    item.item_id,
                    item.cost,
                )
                for item in protector_items
            ],
        )

    # Structure deck shelf.
    async def send_structure_deck_shelf(self):
        decks = (
            getattr(
                GAME,
                "structure_decks",
                [],
            )
            or []
        )

        log.info(
            "[Shop] Structure decks available: %d",
            len(decks),
        )

        await self.send_message(
            0x1C2E,
            items=[shelf_deck(deck) for deck in decks],
        )

    # Dispatches one front request by id; the numbers are the client's request ids (see
    # protocol.md).
    async def handle_front(self, request_id, message):
        fields = message.get("fields", {})
        items = message.get("items", [])

        log.info(
            "[Front] Handling request 0x%04X fields=%r items=%r",
            request_id,
            fields,
            items,
        )

        # 0x2724 keep-alive ping: no reply
        if request_id == 0x2724:
            return

        # 0x1F47 login step 1: account info (0x219A)
        if request_id == 0x1F47:
            await self.send_message(
                0x219A,
                {
                    "regulation_id": REGULATION_ID,
                    "user_id": self.account.user_id,
                    "uname": self.account.username,
                    "dpoint": self.account.duel_points,
                },
            )
            return

        # 0x1F48 login step 2: language, user list (0x2201) and the 0x23F0 premium push
        if request_id == 0x1F48:
            self.lang = as_int(fields.get("lang"), 1)
            self.country = LANG_COUNTRY.get(self.lang, "GB")

            await self.send_message(
                0x2201,
                {"regulation_id": REGULATION_ID},
                [
                    {
                        "user_id": self.account.user_id,
                        "uname": self.account.username,
                        "dpoint": self.account.duel_points,
                        "premium": PREMIUM,
                        "country": self.country,
                    }
                ],
            )

            if "login_push" not in LEGACY:
                await self.send_push(
                    0x23F0,
                    {
                        "dpauth_result": 0,
                        "dpPoints": self.account.duel_points,
                        "dpGetPoints": 0,
                        "premium": PREMIUM,
                        "premium_expire": PREMIUM_EXPIRE,
                    },
                )
            return

        # 0x1F49 character list (0x2260)
        if request_id == 0x1F49:
            await self.send_message(
                0x2260,
                items=[
                    make_player_record(player, self.country)
                    for player in self.account.players
                ],
            )
            return

        # 0x1F4A create a character
        if request_id == 0x1F4A:
            await self.create_player(fields)
            return

        # 0x1F4B delete a character
        if request_id == 0x1F4B:
            await self.delete_player(fields)
            return

        # 0x1F4C select a character
        if request_id == 0x1F4C:
            await self.select_player(fields)
            return

        if self.selected_player is None:
            log.warning(
                "[Front] Ignoring 0x%04X before player selection",
                request_id,
            )
            return

        # 0x2CEE refresh own record
        if request_id == 0x2CEE:
            await self.refresh_player()
            return

        # 0x2CEF save profile edits
        if request_id == 0x2CEF:
            await self.update_player(message)
            return

        # 0x2CF0 player search
        if request_id == 0x2CF0:
            await self.search_player(fields)
            return

        # 0x2CED manner rating of an opponent
        if request_id == 0x2CED:
            await self.rate_player(fields)
            return

        # 0x1F4E deck list
        if request_id == 0x1F4E:
            await self.deck_list()
            return

        # 0x1F4F deck save/upload
        if request_id == 0x1F4F:
            await self.deck_update(message)
            return

        # 0x1F50 deck delete
        if request_id == 0x1F50:
            await self.deck_delete(fields)
            return

        if request_id in (0x1FA4, 0x9000):
            await self.trunk()
            return

        if request_id in (
            0x251C,
            0x252A,
            0x9500,
            0x9514,
            0x11628,
        ):
            await self.send_push(
                request_id + 1 if request_id in (0x9500, 0x9514) else request_id,
                items=[],
            )
            return

        # 0x24C4 Online 2 takeover lists: acknowledged with 0x2530
        if request_id == 0x24C4:
            await self.send_message(0x2530)
            return

        # 0x2072 regulation list
        if request_id == 0x2072:
            regulations = struct.pack(">20H", *range(1, 21))
            await self.send_message(
                0x2398,
                {"regulations": regulations},
            )
            return

        # 0x27D8 card release lists (empty record)
        if request_id == 0x27D8:
            await self.send_message(
                0x27D9,
                items=[{}],
            )
            return

        if request_id in (0x2583, 0x9603):
            await self.mail_count()
            return

        # 0x2582 fetch mail
        if request_id == 0x2582:
            await self.mail_fetch()
            return

        # 0x2580 send mail
        if request_id == 0x2580:
            await self.mail_send(fields)
            return

        if request_id in (0x271A, 0x10010):
            await self.send_message(0x271B if request_id == 0x271A else 0x10011)
            return

        # 0x2710 NPC roster
        if request_id == 0x2710:
            await self.npc_roster()
            return

        if request_id in (0x2AFB, 0x11003):
            await self.team_info()
            return

        if request_id in (0x2AFD, 0x11005):
            await self.team_members()
            return

        # 0x2775 next present (shop purchases arrive as presents)
        if request_id == 0x2775:
            await self.next_present()
            return

        if request_id == 0x10101:
            await self.send_message(0x10104)
            return

        if request_id == 0x1F41:
            await self.send_message(
                0x21FC,
                items=[{"lang": "1"}],
            )
            return

        if request_id in (0x24B8, 0x24C1):
            await self.social_list(request_id)
            return

        if request_id in (
            0x24B9,
            0x24BB,
            0x24BD,
            0x24BF,
            0x24C2,
            0x24C3,
        ):
            await self.social_action(request_id, fields)
            return

        # 0x2135 chat room list
        if request_id == 0x2135:
            await self.room_list()
            return

        # 0x2136 enter chat room
        if request_id == 0x2136:
            await self.enter_room(fields)
            return

        # 0x2137 leave chat room
        if request_id == 0x2137:
            await self.leave_room()
            return

        # 0x2134 chat line
        if request_id == 0x2134:
            await self.chat_message(message)
            return

        if request_id in (0x1B66, 0x7014):
            await self.send_message(
                0x1C20 if request_id == 0x1B66 else 0x7200,
                {"text": "Welcome! Take a look at the packs."},
            )
            return

        # 0x1B67 pack shelf
        if request_id == 0x1B67:
            await self.send_pack_shelf()
            return

        # 0x1B69 avatar shelf
        if request_id == 0x1B69:
            await self.send_avatar_shelf()
            return

        # 0x1B6B protector shelf
        if request_id == 0x1B6B:
            await self.send_protector_shelf()
            return

        # 0x1B6D structure deck shelf
        if request_id == 0x1B6D:
            await self.send_structure_deck_shelf()
            return

        if request_id == 0x1B6F:

            await self.send_message(0x1C26, items=[])
            return

        # 0x1B68 buy an item
        if request_id == 0x1B68:
            await self.shop_buy(fields)
            return

        # 0x1B72 perk count (none)
        if request_id == 0x1B72:
            await self.send_message(
                0x1C36,
                fields={"perk_num": 0},
            )
            return

        # --- card trade and rental scenes ---
        trade_routes = {
            0x2BC0: lambda: self.scene_invite("trade", fields),
            0x2BC4: lambda: self.scene_cancel_invite("trade"),
            0x2BC2: lambda: self.scene_answer("trade", fields),
            0x2BD5: lambda: self.scene_ready("trade"),
            0x2BC6: lambda: self.trade_offer(fields),
            0x2BC7: lambda: self.trade_seen(),
            0x2BC8: lambda: self.trade_status(fields),
            0x2BC9: lambda: self.trade_confirm(fields),
            0x2BCB: lambda: self.scene_leave("trade"),
            0x2C24: lambda: self.scene_invite("rental", fields),
            0x2C28: lambda: self.scene_cancel_invite("rental"),
            0x2C26: lambda: self.scene_answer("rental", fields),
            0x2C3E: lambda: self.scene_ready("rental"),
            0x2C2B: lambda: self.rental_conditions(fields),
            0x2C2A: lambda: self.rental_cards(fields),
            0x2C2C: lambda: self.rental_seen(),
            0x2C2D: lambda: self.rental_confirm(),
            0x2C31: lambda: self.rental_withdraw(),
            0x2C2F: lambda: self.rental_answer(fields),
            0x2C33: lambda: self.scene_leave("rental"),
            0x2C34: lambda: self.rental_records(),
            0x2C35: lambda: self.rental_history(fields),
            0x2D6C: lambda: self.rental_records(),
            # --- teams ---
            0x2AF9: lambda: self.team_form(fields),
            0x2AFE: lambda: self.team_invite(fields),
            0x2B02: lambda: self.team_cancel_invite(),
            0x2B00: lambda: self.team_answer(fields),
            0x2AFA: lambda: self.team_disband_request(),
            0x2B05: lambda: self.team_remove(fields),
            0x2B04: lambda: self.team_leave(),
            0x2B06: lambda: self.team_authority(fields),
            # --- user events ---
            0x339A: lambda: self.event_own(),
            0x339B: lambda: self.event_create_form(),
            0x3390: lambda: self.event_create(fields),
            0x3397: lambda: self.event_enter(fields),
            0x178E: lambda: self.event_leave(fields),
            0x1789: lambda: self.event_search(fields),
            0x177F: lambda: self.event_lobby_info(fields),
            0x3395: lambda: self.event_start(fields),
            0x3393: lambda: self.event_cancel(fields),
        }
        if request_id in trade_routes:
            await trade_routes[request_id]()
            return

        if request_id in (0x32CA, 0x32CC, 0x32CD, 0x32CE, 0x32C9, 0x32D5, 0x32CB):
            await self.event_lobby_query(request_id, fields)
            return

        # 0x2C40 shop rental summary (none)
        if request_id == 0x2C40:
            await self.send_message(
                0x2D75,
                {"rental_cardnum": 0, "rental_point": 0, "rental_term": 0},
            )
            return

        # 0x177A official event list
        if request_id == 0x177A:
            await self.event_list(fields)
            return

        # 0x1793 event result count (none)
        if request_id == 0x1793:

            await self.send_message(0x177B, {"result_number": 0})
            return

        # 0x19C8 empty list reply
        if request_id == 0x19C8:

            await self.send_message(0x19C9, items=[])
            return

        log.info(
            "[Front] Battle Pass diagnostic: request_id=0x%04X fields=%s",
            request_id,
            fields,
        )

        # 0x20D0 Duel Points balance
        if request_id == 0x20D0:
            await self.send_bp_balance()
            return

        # 0x17FC Duel Menu counts
        if request_id == 0x17FC:
            await self.send_message(
                0x17FD,
                {
                    "duel_playing": 0,
                    "duel_ready": 0,
                    "duel_auto": 0,
                },
            )
            return

        # 0x1784 duel/event entry: hands out a join id
        if request_id == 0x1784:
            self.npc_event_id = fields.get("event_id")
            self.npc_join_id = next_yo3_id("join")
            self.npc_duel_id = None
            self.npc_id = None
            self.npc_native_duel_id = None

            reply = {
                "join_id": self.npc_join_id,
                "server_id": as_int(fields.get("server_id")),
                "event_score": 0,
                "duel_password_flag": as_int(fields.get("duel_password_flag")),
            }
            # Only the NPC event's id and a user event's go back: echoing
            # Auto Duel's id made clients queue again after each match.
            if (
                as_int(self.npc_event_id) == EVENT_ID
                or as_int(self.npc_event_id) in STATE.user_events
            ):
                reply["event_id"] = self.npc_event_id
            await self.send_message(0x1785, reply)
            return

        # 0x17AC host/NPC duel setup: hands out a duel id
        if request_id == 0x17AC:
            if (
                self.npc_event_id != EVENT_ID
                or self.npc_join_id is None
                or fields.get("join_id") != self.npc_join_id
            ):
                await self.send_message(0x17AE)
                return

            item = message["items"][0] if message.get("items") else {}

            npc_id = record_fields(item).get("cpu_id")

            if not isinstance(npc_id, int) or npc_id <= 0:
                await self.send_message(0x17AE)
                return

            self.npc_id = npc_id
            self.npc_duel_id = next_yo3_id("duel")

            await self.send_message(
                0x17AD,
                {
                    "duel_id": self.npc_duel_id,
                    "regulation_id": REGULATION_ID,
                    "country": EVENT_COUNTRY,
                },
            )
            return

        # 0x1798 auto-match request (placeholder pairing)
        if request_id == 0x1798:
            if FRONT_MATCHMAKER:
                deck_payload = fields.get("deck", b"")

                if not deck_payload and GAME.starter_deck:
                    deck_payload = preset_payload(GAME.starter_deck)

                opponent_id = 999 if self.selected_player != 999 else 998

                duel_id = FRONT_MATCHMAKER.duel_manager.create_duel(
                    player1_id=self.selected_player,
                    player2_id=opponent_id,
                    deck1=deck_payload,
                    deck2=deck_payload,
                )

                if duel_id:
                    conn_packet = FRONT_MATCHMAKER.build_connection_packet(duel_id)
                    await self.send_raw(conn_packet)
            return

        # 0x17B6 challenge a hosted duel (placeholder)
        if request_id == 0x17B6:
            if FRONT_MATCHMAKER:
                FRONT_MATCHMAKER.handle_host_challenge(
                    self.selected_player,
                    fields,
                )

                await self.send_message(
                    0x17B7,
                    {"result": 1},
                )
            return

        # 0x17CA host's answer to a challenge
        if request_id == 0x17CA:
            if FRONT_MATCHMAKER:
                host_id = fields.get("host_id", 0)
                accept = fields.get("accept", True)
                deck_payload = fields.get("deck", b"")

                duel_id = FRONT_MATCHMAKER.handle_accept_reject_challenge(
                    self.selected_player,
                    host_id,
                    accept,
                    deck_payload,
                )

                if duel_id:
                    conn_packet = FRONT_MATCHMAKER.build_connection_packet(duel_id)

                    await self.send_raw(conn_packet)

                    push_fields, push_items = duel_announce_payload(
                        FRONT_ADDRESS,
                        DUEL_PORT,
                        zlib.crc32(duel_id.encode("ascii")) & 0x7FFFFFFF,
                        "Opponent",
                    )

                    STATE.online_push(
                        host_id,
                        0x1839,
                        push_fields,
                        push_items,
                    )
            return

        # 0x17D4 duel deck confirmed: announce the duel server
        if request_id == 0x17D4:
            if (
                self.npc_join_id is None
                or fields.get("join_id") != self.npc_join_id
                or fields.get("duel_id") != self.npc_duel_id
                or not self.npc_native_duel_id
            ):
                await self.send_message(0x17D6)
                return

            await self.send_message(0x17D5)
            await self.announce_duel()
            return

        # 0x1838 re-announce the duel server
        if request_id == 0x1838:
            await self.announce_duel()
            return

        if request_id in AUX_SUCCESS_REPLY:
            await self.send_message(AUX_SUCCESS_REPLY[request_id])
            return

        log.warning(
            "[Front] Unhandled request 0x%04X",
            request_id,
        )

    # Reads the client's version string and answers: the gate sends the front address, the front
    # 0x219F.
    async def opener(self):
        packet = await asyncio.wait_for(
            read_frame(self.reader),
            timeout=10,
        )

        if packet.startswith(VERSION):
            log.info(
                "[%s] Client version verified",
                self.service,
            )
        else:
            log.warning(
                "[%s] Unexpected initial payload: %s",
                self.service,
                packet.hex(" "),
            )

        if self.service == "Gate":
            await self.send_message(
                0x21FE,
                {
                    "host": FRONT_ADDRESS,
                    "port": FRONT_PORT,
                },
            )
        else:
            await self.send_message(0x219F)

    # Connection loop: read, decode, dispatch; cleans up presence, rooms and scenes on exit.
    async def handle(self):
        log.info(
            "[%s] Connection from %s",
            self.service,
            self.addr,
        )

        try:
            await self.opener()

            while not self.closed:
                packet = await asyncio.wait_for(
                    read_frame(self.reader),
                    timeout=180,
                )

                log.info(
                    "[%s] RAW incoming packet (%d bytes): %s",
                    self.service,
                    len(packet),
                    packet[:128].hex(" "),
                )

                message, consumed = decode_message(packet)

                fields = message.get("fields", {})
                request_id = fields.get("request")

                log.info(
                    "[%s] Request %s fields=%r items=%r",
                    self.service,
                    hex(request_id) if isinstance(request_id, int) else None,
                    fields,
                    message.get("items", []),
                )

                if not isinstance(request_id, int):
                    continue

                if self.service == "Front":
                    await self.handle_front(
                        request_id,
                        message,
                    )

        except (
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
        ):
            log.info(
                "[%s] Disconnected or timed out",
                self.service,
            )

        except Exception:
            log.exception(
                "[%s] Session error",
                self.service,
            )

        finally:
            self.closed = True

            if (
                self.selected_player is not None
                and STATE.online.get(self.selected_player) is self
            ):
                STATE.online.pop(
                    self.selected_player,
                    None,
                )
                notify_friend_presence(self.selected_player)
                player_left(self.selected_player)

            if self.room_id is not None:
                STATE.chat_rooms.get(
                    self.room_id,
                    {},
                ).get(
                    "members",
                    set(),
                ).discard(self.selected_player)

            STATE.save()

            self.writer.close()

            try:
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass

            log.info(
                "[%s] Connection closed",
                self.service,
            )

    # 0x1F4A: creates a player character (unique name and index, up to 4).
    async def create_player(self, fields):
        name = fields.get("pname")
        index = fields.get("pindex")
        if not isinstance(name, str) or not isinstance(index, int):
            await self.send_message(0x2263)
            return
        name = name.strip()
        if not 1 <= len(name) <= 16 or len(self.account.players) >= MAX_PLAYERS:
            await self.send_message(0x2263)
            return
        if any(
            p["pname"].lower() == name.lower() or p["pindex"] == index
            for p in self.account.players
        ):
            await self.send_message(0x2263)
            return
        new_id = max((p["player_id"] for p in self.account.players), default=0) + 1
        self.account.players.append(make_player(new_id, name, index))
        STATE.save()
        await self.send_message(0x2262)

    # 0x1F4B: deletes a character (never the last one) and takes it out of its team.
    async def delete_player(self, fields):
        player_id = fields.get("player_id")
        player = self.account.find_player(player_id)
        if player is None or len(self.account.players) <= 1:
            await self.send_message(0x2265)
            return
        self.account.players.remove(player)
        team_drop_member(player_id)
        player_left(player_id)
        if self.selected_player == player_id:
            STATE.online.pop(player_id, None)
            self.selected_player = None
        STATE.save()
        await self.send_message(0x2264)

    # 0x1F4C: picks the character to play; registers it online.
    async def select_player(self, fields):
        player_id = fields.get("player_id")
        player = self.account.find_player(player_id)
        if player is None:
            return
        if (
            self.selected_player is not None
            and STATE.online.get(self.selected_player) is self
        ):
            STATE.online.pop(self.selected_player, None)
            player_left(self.selected_player)
        self.selected_player = player_id
        STATE.online[player_id] = self
        await self.send_message(0x2266)
        notify_friend_presence(player_id)

    # 0x2CEE: re-sends the selected player's record.
    async def refresh_player(self):
        player = self.player()
        if player is None:
            await self.send_message(0x2CF4)
            return
        await self.send_message(
            0x2CF3, items=[make_player_record(player, self.country)]
        )

    # 0x2CEF: saves the player's profile edits (comments, speech, avatar).
    async def update_player(self, message):
        player = self.player()
        if player is None:
            await self.send_message(0x2CF6)
            return
        fields = message["fields"]
        if message["items"]:
            nested = record_fields(message["items"][0])
            if "avatar1" in nested:
                fields = nested
        avatar = fields.get("avatar1")
        if not isinstance(avatar, bytes) or len(avatar) > 256:
            await self.send_message(0x2CF6)
            return
        player["avatar1"] = avatar
        if isinstance(fields.get("title"), int):
            player["title"] = fields["title"]
        for i in range(2):
            key = f"comment{i:02d}"
            if isinstance(fields.get(key), str):
                player[key] = clip_text(fields[key], 200)
        for i in range(23):
            key = f"speech{i:02d}"
            if isinstance(fields.get(key), str):
                player[key] = clip_text(fields[key], 200)
        STATE.save()
        await self.send_message(0x2CF5)

    async def rate_player(self, fields):
        """0x2CED {eval_id, eval_data} -> 0x2CF1 (0x2CF2 {error_code}).

        The evaluation was asked for by 0x2CEC after a duel between players:
        it counts once, from the rater it names, with one of the five button
        values (-10, -5, 0, 5, 10; sent as a u32, so -10 arrives huge).
        """
        value = as_int(fields.get("eval_data"))
        if value >= 0x80000000:
            value -= 0x100000000
        evaluation = STATE.evaluations.get(as_int(fields.get("eval_id")))
        rated = STATE.player(evaluation["rated"]) if evaluation else None
        if (
            evaluation is None
            or evaluation["rater"] != self.selected_player
            or rated is None
            or value not in EVALUATION_VALUES
        ):
            await self.send_message(0x2CF2, {"error_code": 700})
            return
        del STATE.evaluations[as_int(fields.get("eval_id"))]
        STATE.ratings.setdefault(self.selected_player, {})[evaluation["rated"]] = value
        rated["eval_num"] = int(rated.get("eval_num", 0)) + 1
        rated["eval_total"] = int(rated.get("eval_total", 0)) + value
        STATE.save()
        await self.send_message(0x2CF1)

    # A player as a search result row (record, duel and event status, team).
    def search_entry(self, player_id):
        player = STATE.player(player_id)
        session = STATE.online.get(player_id)
        team_id, team = team_of(player_id)
        return {
            "player_id": player_id,
            "pname": player["pname"],
            "dlv": player["dlv"],
            "n_win": int(player.get("n_win", 0)),
            "n_lose": int(player.get("n_lose", 0)),
            "n_draw": int(player.get("n_draw", 0)),
            "duel_id": 0,
            "event_score": 0,
            "duel_status": 0,
            "tprank": 0,
            "duel_match_type": 0,
            "world_evid": 0,
            "official_evid": 0,
            "user_evid": (uev_of_player(player_id) or {}).get("event_id", 0),
            "userevent_remain": 1 if uev_of_player(player_id) else 0,
            "team_id": team_id or 0,
            "team_name": team["name"] if team else "",
            "team_authority": team["members"].get(player_id, 0) if team else 0,
            "country": session.country if session else "GB",
            "evallevel": eval_level(player),
        }

    async def search_player(self, fields):
        """0x2CF0 -> 0x2CF7 {result_number} + items. Filters follow the
        search_flag bits of the protocol; paging (base_point, front_num) is a guess."""
        me = self.selected_player
        flag = as_int(fields.get("search_flag"), 1)
        base = as_int(fields.get("base_point"))
        front = as_int(fields.get("front_num"), 100) or 100

        if flag & 0x40000:
            target = as_int(fields.get("player_id"))
            ids = [target] if STATE.player(target) is not None else []
            entries = [self.search_entry(pid) for pid in ids]
            await self.send_message(0x2CF7, {"result_number": len(entries)}, entries)
            return

        if flag & 0x20 and not flag & (0x40 | 0x80 | 0x20000):
            # The Team tab: the player's own team, themselves included.
            _, my_team_data = team_of(me)
            ids = [p for p in (my_team_data or {"members": {}})["members"] if STATE.player(p)]
            await self.send_message(
                0x2CF7,
                {"result_number": len(ids)},
                [self.search_entry(p) for p in ids[base : base + front]],
            )
            return

        if flag & (0x200 | 0x400 | 0x800 | 0x1000 | 0x2000 | 0x4000 | 0x8000):
            await self.send_message(0x2CF7, {"result_number": 0}, [])
            return

        community = flag & (0x40 | 0x80 | 0x20 | 0x20000)
        my_team, _ = team_of(me)
        name = fields.get("pname", "")
        name = name.lower() if isinstance(name, str) else ""
        lang_mask = as_int(fields.get("lang"))
        country = fields.get("country", "")
        eval_mask = as_int(fields.get("eval_level"))
        low = as_int(fields.get("min_level"))
        high = as_int(fields.get("max_level"), 0xFFFF)

        matches = []
        for player_id in sorted(STATE.online):
            player = STATE.player(player_id)
            if player is None or player_id == me:
                continue
            if flag & 0x2 and name and name not in player["pname"].lower():
                continue
            if flag & 0x10 and not low <= player["dlv"] <= high:
                continue
            session = STATE.online[player_id]
            if flag & 0x4 and not lang_mask >> session.lang & 1:
                continue
            if flag & 0x8 and country and session.country != country:
                continue
            if flag & 0x100 and not eval_mask >> eval_level(player) & 1:
                continue
            if community:
                in_group = (
                    (flag & 0x40 and player_id in self.friend_set(me))
                    or (flag & 0x80 and player_id in self.ignore_set(me))
                    or (
                        flag & 0x20
                        and my_team is not None
                        and STATE.player_teams.get(player_id) == my_team
                    )
                    or (
                        flag & 0x20000
                        and self.room_id is not None
                        and session.room_id == self.room_id
                    )
                )
                if not in_group:
                    continue
            matches.append(player_id)

        page = matches[base : base + front]
        await self.send_message(
            0x2CF7,
            {"result_number": len(matches)},
            [self.search_entry(player_id) for player_id in page],
        )

    # 0x1F4E: the player's decks.
    async def deck_list(self):
        player = self.player()
        if player is None:
            return
        decks, names = [], set()

        def add(deck):
            if len(decks) < MAX_DECKS and deck.deck_name not in names:
                names.add(deck.deck_name)
                decks.append(deck)

        if GAME.starter_deck is not None:
            add(preset_as_deck(GAME.starter_deck, STARTER_DECK_ID, STARTER_DECK_NAME))
        for saved in player.get("decks", []):
            add(saved)
        for name, preset in GAME.official_decks:
            add(preset_as_deck(preset, preset.deck_id, name))
        await self.send_message(0x22C8, items=[deck.record() for deck in decks])

    # 0x1F4F: saves a deck; with the NPC duel's id in the request it also builds that duel (the
    # player's deck against the NPC's preset).
    async def deck_update(self, message):
        player = self.player()

        if player is None or not message.get("items"):
            await self.send_message(0x22CA)
            return

        try:
            deck = Deck.from_record(message["items"][0])
        except Exception:
            log.exception("[Deck] Failed to read uploaded deck")
            await self.send_message(0x22CB)
            return

        if not deck.deck_name:
            await self.send_message(0x22CA)
            return

        fields = message.get("fields", {})

        if self.npc_duel_id is not None and fields.get("duel_id") == self.npc_duel_id:
            if self.npc_id is None or FRONT_MATCHMAKER is None:
                log.error(
                    "[NPC] Cannot create duel: npc_id=%s, matchmaking=%s",
                    self.npc_id,
                    FRONT_MATCHMAKER is not None,
                )
                await self.send_message(0x22CB)
                return

            if not self.npc_native_duel_id:
                npc_preset = GAME.find_npc_deck(self.npc_id)

                if npc_preset is None:
                    log.error(
                        "[NPC] No preset deck found for NPC %s",
                        self.npc_id,
                    )
                    await self.send_message(0x22CB)
                    return

                player_payload = {
                    "main": deck.main,
                    "extra": deck.extra,
                    "side": deck.side,
                }

                npc_payload = preset_payload(npc_preset)
                npc_virtual_id = 0x70000000 + self.npc_id

                try:
                    log.info(
                        "[NPC] Creating native duel: player=%s, NPC=%s",
                        self.selected_player,
                        self.npc_id,
                    )

                    self.npc_native_duel_id = FRONT_MATCHMAKER.duel_manager.create_duel(
                        player1_id=self.selected_player,
                        player2_id=npc_virtual_id,
                        deck1=player_payload,
                        deck2=npc_payload,
                        npc_player_id=npc_virtual_id,
                    )

                    log.info(
                        "[NPC] Native duel successfully created: %s",
                        self.npc_native_duel_id,
                    )

                except Exception:
                    log.exception(
                        "[NPC] Native duel creation failed " "(player=%s, NPC=%s)",
                        self.selected_player,
                        self.npc_id,
                    )
                    await self.send_message(0x22CB)
                    return

        decks = player.setdefault("decks", [])

        if fields.get("update_type", 0) == 1 and deck.deck_name not in preset_names():
            decks[:] = [old for old in decks if old.deck_name != deck.deck_name]

            decks.append(deck)

            if len(decks) > MAX_DECKS:
                decks.pop(0)

        try:
            STATE.save()
        except Exception:
            log.exception("[Deck] Failed to save player deck")
            await self.send_message(0x22CB)
            return

        await self.send_message(0x22CA)

    # 0x1F50: deletes a deck.
    async def deck_delete(self, fields):
        player = self.player()
        if player is None:
            return
        name = fields.get("deck_name", "")
        if isinstance(name, str):
            player["decks"] = [
                deck for deck in player.get("decks", []) if deck.deck_name != name
            ]
        STATE.save()
        await self.send_message(0x22CC)

    # The player's friend ids (created on demand).
    def friend_set(self, player_id):
        return STATE.friends.setdefault(player_id, set())

    # The player's ignore-list ids (created on demand).
    def ignore_set(self, player_id):
        return STATE.ignored.setdefault(player_id, set())

    # Friend (0x24B8) or ignore (0x24C1) list.
    async def social_list(self, request_id):
        if request_id == 0x24B8:
            await self.send_message(
                0x251C, items=friend_list_items(self.selected_player)
            )
        else:
            await self.send_message(
                0x252A, items=ignore_list_items(self.selected_player)
            )

    # Friend requests, answers, cancels, removal and ignore/unignore.
    async def social_action(self, request_id, fields):
        me = self.selected_player
        mine = self.player()
        target = as_int(fields.get("to_id"))
        other = STATE.player(target)

        if mine is None:
            return

        friends = self.friend_set(me)
        ignored = self.ignore_set(me)

        if request_id == 0x24B9:
            if other is None or target == me:
                await self.send_message(0x251F, {"error_code": 700})
            elif target not in STATE.online:
                await self.send_message(0x251F, {"error_code": 112})
            elif len(friends) >= MAX_FRIENDS:
                await self.send_message(0x251F, {"error_code": 110})
            elif target in friends or STATE.friend_requests.get(target):
                await self.send_message(0x251F, {"error_code": 106})
            else:
                STATE.friend_requests.setdefault(target, set()).add(me)
                await self.send_message(0x251E)
                STATE.online_push(
                    target, 0x24BA, {"from_id": me, "from_name": mine["pname"]}
                )
            return

        if request_id == 0x24BB:
            pending = STATE.friend_requests.setdefault(me, set())
            answer = as_int(fields.get("answer"), 1)
            if other is None or target not in pending:
                await self.send_message(0x2523, {"error_code": 700})
                return
            pending.discard(target)
            if answer == 2:
                friends.add(target)
                self.friend_set(target).add(me)
                STATE.save()
            await self.send_message(
                0x2522,
                {
                    "answer": answer,
                    "friend_id": target,
                    "friend_name": other["pname"],
                },
            )
            STATE.online_push(
                target,
                0x24BC,
                {"answer": answer, "friend_id": me, "friend_name": mine["pname"]},
            )
            return

        if request_id == 0x24BD:
            for waiting_id, pending in list(STATE.friend_requests.items()):
                if me in pending:
                    pending.discard(me)
                    STATE.online_push(waiting_id, 0x24BE)
            await self.send_message(0x2524)
            return

        if request_id == 0x24BF:
            if other is None:
                await self.send_message(0x2529, {"error_code": 700})
                return
            friends.discard(target)
            self.friend_set(target).discard(me)
            STATE.save()
            await self.send_message(0x2528, {"to_id": target})
            STATE.online_push(target, 0x2528, {"to_id": me})
            return

        if request_id == 0x24C2:
            if other is None or target == me:
                await self.send_message(0x252D, {"error_code": 700})
                return
            ignored.add(target)
            STATE.save()
            await self.send_message(
                0x252C, {"to_id": target, "to_name": other["pname"]}
            )
            return

        if request_id == 0x24C3:
            ignored.discard(target)
            STATE.save()
            await self.send_message(0x252E, {"to_id": target})
            return

    # 0x2582: delivers and clears the mailbox.
    async def mail_fetch(self):
        mailbox = STATE.mailboxes.setdefault(self.selected_player, [])
        items = [mail_record(mail) for mail in mailbox]
        mailbox.clear()
        STATE.save()
        await self.send_message(0x2586, items=items)

    # 0x2580: sends a D-Mail to a player or, with type 3, to the sender's team.
    async def mail_send(self, fields):
        sender = self.player()
        if sender is None:
            return

        mail_type = as_int(fields.get("mail_type"))
        title = fields.get("title", "")
        body = fields.get("body", "")
        title = title[:MAIL_TITLE_MAX] if isinstance(title, str) else ""
        body = body[:MAIL_BODY_MAX] if isinstance(body, str) else ""

        if mail_type == 3:
            team_id, team = team_of(self.selected_player)
            if team is None:
                await self.send_message(0x2585, {"error_code": 700})
                return
            recipients = [pid for pid in team["members"] if pid != self.selected_player]
            team_name = team.get("name", "")
        else:
            team_id, team_name = 0, ""
            to_id = as_int(fields.get("to_id"))
            if STATE.player(to_id) is None:
                await self.send_message(0x2585, {"error_code": 301})
                return
            recipients = [to_id]

        sent = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        first_id = STATE.next_mail_id

        for recipient_id in recipients:
            recipient = STATE.player(recipient_id)
            if recipient is None:
                continue
            STATE.mailboxes.setdefault(recipient_id, []).append(
                {
                    "mail_id": STATE.next_mail_id,
                    "mail_type": 3 if mail_type == 3 else 0,
                    "from_id": self.selected_player,
                    "from_name": sender["pname"],
                    "to_id": recipient_id,
                    "to_name": recipient["pname"],
                    "title": title,
                    "body": body,
                    "send_date": sent,
                    "team_id": team_id or 0,
                    "team_name": team_name,
                }
            )
            STATE.next_mail_id += 1
            STATE.online_push(recipient_id, 0x2581)

        STATE.save()
        await self.send_message(0x2584, {"mail_id": first_id})

    # 0x2135: the chat rooms.
    async def room_list(self):

        rooms = [
            {
                "id": room_id,
                "name": room["name"],
                "capacity": 50,
                "num": len(room.get("members", ())),
                "region": 0,
                "status": 0,
                "type": 0,
            }
            for room_id, room in sorted(STATE.chat_rooms.items())
        ]
        await self.send_message(0x2458, {"lang": 1}, rooms)

    # 0x2136: joins a chat room.
    async def enter_room(self, fields):
        room_id = as_int(fields.get("room_id"))
        if room_id not in STATE.chat_rooms:
            await self.send_message(0x245B, {"error_code": 1})
            return
        if self.room_id is not None:
            STATE.chat_rooms.get(self.room_id, {}).get("members", set()).discard(
                self.selected_player
            )
        STATE.chat_rooms[room_id].setdefault("members", set()).add(self.selected_player)
        self.room_id = room_id
        await self.send_message(0x245A, {"room_id": room_id})

    # 0x2137: leaves the chat room.
    async def leave_room(self):
        if self.room_id is not None:
            STATE.chat_rooms.get(self.room_id, {}).get("members", set()).discard(
                self.selected_player
            )
        self.room_id = None
        await self.send_message(0x245C)

    async def chat_message(self, message):
        """0x2134: the fields sit in one item (tag 10): chat_str, chat_type and
        to_id (room id / team id) or to_name (whisper)."""
        sender = self.player()
        if sender is None:
            return

        data = dict(message.get("fields", {}))
        for item in message.get("items", []):
            data.update(record_fields(item))

        text = clip_text(data.get("chat_str", ""), 512)
        chat_type = as_int(data.get("chat_type"), 1)

        if not text:
            await self.send_message(0x2457, {"error_code": 700})
            return

        recipients = set()
        if chat_type == 3:
            _, team = team_of(self.selected_player)
            if team is not None:
                recipients = set(team["members"])
        elif isinstance(data.get("to_name"), str) and data["to_name"]:
            wanted = data["to_name"].lower()
            for player_id in STATE.online:
                player = STATE.player(player_id)
                if player and player["pname"].lower() == wanted:
                    recipients = {player_id, self.selected_player}
                    break
            if not recipients:
                await self.send_message(0x2457, {"error_code": 112})
                return
        else:
            if self.room_id is None:
                await self.send_message(0x2457, {"error_code": 700})
                return
            recipients = set(
                STATE.chat_rooms.get(self.room_id, {}).get("members", set())
            )

        recipients.add(self.selected_player)
        line = {
            "from_id": self.selected_player,
            "pname": sender["pname"],
            "chat_type": chat_type,
            "chat_str": text,
        }
        for player_id in recipients:
            STATE.online_push(player_id, 0x2455, items=[dict(line)])

        await self.send_message(0x2456)

    # 0x177A: the official event list (one configurable entry).
    async def event_list(self, fields):
        entry = {"event_type": EVENT_TYPE, "event_id": EVENT_ID, "status": EVENT_STATUS}
        if EVENT_LAYOUT == "fields":
            await self.send_message(0x177B, entry)
        elif EVENT_LAYOUT == "empty":
            await self.send_message(0x177B, {"result_number": 0})
        else:
            await self.send_message(0x177B, items=[entry])

    # Announces the NPC duel server (0x1839 and/or 0x22C4).
    async def announce_duel(self):
        if self.npc_duel_id is None or not self.npc_native_duel_id:
            await self.send_message(0x183A)
            return
        player = self.player() or {}
        avatar = player.get("avatar1", b"\x00" * 82)
        fields, items = duel_announce_payload(
            FRONT_ADDRESS,
            DUEL_PORT,
            self.npc_duel_id,
            "NPC",
            avatar_1=avatar,
            avatar_2=b"\x00" * len(avatar),
        )
        if DUEL_ANNOUNCE in ("both", "1839"):
            await self.send_message(0x1839, fields, items)
        if DUEL_ANNOUNCE in ("both", "22c4"):
            await self.send_message(
                0x22C4, items=[duel_data_item(FRONT_ADDRESS, DUEL_PORT)]
            )

    # 0x2710: the NPCs and their levels.
    async def npc_roster(self):

        if "roster" in LEGACY:
            await self.send_message(
                0x2711,
                items=[
                    {
                        "npc_type": min((i - 120) // 3, 2),
                        "npc_id": i,
                        "need_ex": 100 * ((i % 3) + 1),
                    }
                    for i in range(120, 195)
                ],
            )
            return
        items = []
        for first_id in range(120, 195, 3):
            index = (first_id - 120) // 3
            npc_type = 0 if index < 9 else 1 if index < 17 else 2
            for level in range(3):
                items.append(
                    {
                        "npc_type": npc_type,
                        "npc_id": first_id + level,
                        "need_ex": 100 * (level + 1),
                    }
                )
        await self.send_message(0x2711, items=items)

    # 0x2AFB: the player's team (0x2B13), or 0x2B14 404 without one.
    async def team_info(self):
        team_id, team = team_of(self.selected_player)
        if team is None:

            await self.send_message(0x2B14, {"error_code": 404})
            return
        leader = next(
            (
                STATE.player(pid)["pname"]
                for pid, authority in team["members"].items()
                if authority == 2 and STATE.player(pid)
            ),
            "",
        )
        await self.send_message(
            0x2B13,
            {
                "team_id": team_id,
                "team_name": team["name"],
                "level": 1,
                "leader_name": leader,
                "created": team.get("created", "2010-03-23 00:00:00"),
                "season_score": 0,
                "score": 0,
            },
        )

    # 0x2AFD: the team's members (0x2B17), or 0x2B18 without a team.
    async def team_members(self):
        team_id, team = team_of(self.selected_player)
        if team is None:
            await self.send_message(0x2B18, {"error_code": 404})
            return
        items = [
            {
                "member_id": pid,
                "member_name": STATE.player(pid)["pname"],
                "login_flag": 1 if pid in STATE.online else 0,
                "authority": authority,
            }
            for pid, authority in team["members"].items()
            if STATE.player(pid)
        ]
        await self.send_message(0x2B17, items=items)

    # ------------------------------------------------------------------
    # Teams: forming, inviting, answering, leaving, removing, authority
    # ------------------------------------------------------------------

    async def team_form(self, fields):
        """0x2AF9 {team_name} -> 0x2B0F {team_name}; 0x2B10 {error_code}:
        409 the name is taken, 808 bad characters, 700 anything else."""
        me = self.selected_player
        name = fields.get("team_name")
        name = name.strip() if isinstance(name, str) else ""

        if me is None or not self.account.premium or team_of(me)[1] is not None:
            await self.send_message(0x2B10, {"error_code": 700})
            return
        if not valid_team_name(name):
            await self.send_message(0x2B10, {"error_code": 808})
            return
        if team_name_taken(name):
            await self.send_message(0x2B10, {"error_code": 409})
            return

        team_id = STATE.next_team_id
        STATE.next_team_id += 1
        STATE.teams[team_id] = {
            "name": name,
            "created": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "members": {me: TEAM_LEADER},
        }
        STATE.player_teams[me] = team_id
        STATE.save()
        await self.send_message(0x2B0F, {"team_name": name})

    async def team_invite(self, fields):
        """0x2AFE {to_id} -> 0x2B19; the invited player's bell gets 0x2AFF.
        0x2B1A {error_code}: 106 the player is in a team or already invited,
        700 anything else (offline, no right to invite)."""
        me = self.selected_player
        target = as_int(fields.get("to_id"))
        team_id, team = team_of(me)
        mine = self.player()

        async def refuse(code):
            await self.send_message(0x2B1A, {"error_code": code})

        if (
            mine is None
            or team is None
            or team["members"].get(me, TEAM_MEMBER) < TEAM_SUBLEADER
            or target == me
            or STATE.player(target) is None
            or target not in STATE.online
        ):
            await refuse(700)
        elif team_of(target)[1] is not None or target in STATE.team_invites:
            await refuse(106)
        else:
            STATE.team_invites[target] = me
            await self.send_message(0x2B19)
            STATE.online_push(
                target,
                0x2AFF,
                {
                    "from_id": me,
                    "from_name": mine["pname"],
                    "team_id": team_id,
                    "team_name": team["name"],
                },
            )

    async def team_cancel_invite(self):
        """0x2B02 (bare) -> 0x2B1F; every invited player's bell clears (0x2B03)."""
        me = self.selected_player
        for invitee, inviter in list(STATE.team_invites.items()):
            if inviter == me:
                del STATE.team_invites[invitee]
                STATE.online_push(invitee, 0x2B03)
        await self.send_message(0x2B1F)

    async def team_answer(self, fields):
        """0x2B00 {to_id: the inviter, team_id, answer: 2 yes / 1 no} -> 0x2B1D
        (0x2B1E {error_code} for a stale invitation). The inviter gets 0x2B01
        {answer}; on a yes the members get 0x2B08 {member_name}."""
        me = self.selected_player
        mine = self.player()
        inviter = as_int(fields.get("to_id"))
        wanted_team = as_int(fields.get("team_id"))
        answer = as_int(fields.get("answer"), 1)

        team_id, team = team_of(inviter)
        valid = (
            mine is not None
            and STATE.team_invites.get(me) == inviter
            and team is not None
            and (not wanted_team or wanted_team == team_id)
            and team_of(me)[1] is None
        )
        STATE.team_invites.pop(me, None)
        if not valid:
            await self.send_message(0x2B1E, {"error_code": 700})
            return

        if answer == 2:
            team["members"][me] = TEAM_MEMBER
            STATE.player_teams[me] = team_id
            STATE.save()
        await self.send_message(0x2B1D)
        STATE.online_push(inviter, 0x2B01, {"answer": 2 if answer == 2 else 1})
        if answer == 2:
            team_push(team, 0x2B08, {"member_name": mine["pname"]}, skip={me})

    async def team_disband_request(self):
        """0x2AFA {player_id} (the leader) -> 0x2B11 (0x2B12 {error_code});
        the others get 0x2B07 {team_name} and lose the team."""
        me = self.selected_player
        team_id, team = team_of(me)
        if team is None or team["members"].get(me) != TEAM_LEADER:
            await self.send_message(0x2B12, {"error_code": 700})
            return
        await self.send_message(0x2B11)
        team_push(team, 0x2B07, {"team_name": team["name"]}, skip={me})
        team_disband(team_id)

    async def team_remove(self, fields):
        """0x2B05 {to_id} -> 0x2B25 (0x2B26 {error_code}). The leader removes
        anyone, a subleader only plain members. Everyone, the one removed
        too, gets 0x2B0A {admin_name, member_name}."""
        me = self.selected_player
        mine = self.player()
        target = as_int(fields.get("to_id"))
        team_id, team = team_of(me)
        mine_auth = team["members"].get(me, TEAM_MEMBER) if team else TEAM_MEMBER
        target_auth = team["members"].get(target) if team else None

        if (
            mine is None
            or team is None
            or target == me
            or target_auth is None
            or mine_auth < TEAM_SUBLEADER
            or mine_auth <= target_auth
        ):
            await self.send_message(0x2B26, {"error_code": 700})
            return
        await self.send_message(0x2B25)
        team_push(
            team,
            0x2B0A,
            {"admin_name": mine["pname"], "member_name": uev_name(target)},
        )
        team_drop_member(target)

    async def team_leave(self):
        """0x2B04 (bare, not the leader) -> 0x2B23 (0x2B24 {error_code});
        the others get 0x2B09 {member_name}."""
        me = self.selected_player
        mine = self.player()
        team_id, team = team_of(me)
        if mine is None or team is None or team["members"].get(me) == TEAM_LEADER:
            await self.send_message(0x2B24, {"error_code": 700})
            return
        await self.send_message(0x2B23)
        team_push(team, 0x2B09, {"member_name": mine["pname"]}, skip={me})
        team_drop_member(me)

    async def team_authority(self, fields):
        """0x2B06 {to_id, authority} (the leader) -> 0x2B27 (0x2B28 {error_code}).
        1 subleader, 0 back to member, 2 hands the leadership over (the old
        leader becomes a member, a guess). Everyone gets 0x2B0B {authority, to_name}."""
        me = self.selected_player
        target = as_int(fields.get("to_id"))
        authority = as_int(fields.get("authority"), -1)
        team_id, team = team_of(me)

        if (
            team is None
            or team["members"].get(me) != TEAM_LEADER
            or target == me
            or target not in team["members"]
            or authority not in (TEAM_MEMBER, TEAM_SUBLEADER, TEAM_LEADER)
        ):
            await self.send_message(0x2B28, {"error_code": 700})
            return
        team["members"][target] = authority
        if authority == TEAM_LEADER:
            team["members"][me] = TEAM_MEMBER
        STATE.save()
        await self.send_message(0x2B27)
        team_push(team, 0x2B0B, {"authority": authority, "to_name": uev_name(target)})

    # ------------------------------------------------------------------
    # Trade and rental scenes: invitations shared by both kinds
    # ------------------------------------------------------------------

    def active_scene(self, kind):
        """The scene of this kind the selected player sits in, or None."""
        scene = STATE.scenes.get(self.selected_player)
        return scene if scene is not None and scene["kind"] == kind else None

    async def send_collection(self):
        """The player's trunk (9000 = 0x2328) alone, as after a finished trade."""
        player = self.player()
        if player is None:
            return
        cards, no_trade = build_trunk(player)
        await self.send_message(0x2328, items=[{"cards": cards, "noTrade": no_trade}])

    async def scene_invite(self, kind, fields):
        """Trade 0x2BC0 / rental 0x2C24 {to_id} -> {pname} (error {error_code 106}:
        "This player cannot receive another request."). The player's bell gets
        {from_id, from_name}. One scene at a time: nobody in a scene may invite or be invited."""
        ids = SCENE_IDS[kind]
        me = self.selected_player
        mine, target = self.player(), as_int(fields.get("to_id"))
        other = STATE.player(target)
        invites = STATE.scene_invites[kind]

        if (
            mine is None
            or other is None
            or target == me
            or target not in STATE.online
            or me in STATE.scenes
            or target in STATE.scenes
            or target in invites
        ):
            await self.send_message(ids["invite_err"], {"error_code": 106})
            return
        invites[target] = me
        await self.send_message(ids["invite_ok"], {"pname": other["pname"]})
        STATE.online_push(
            target, ids["invite_push"], {"from_id": me, "from_name": mine["pname"]}
        )

    async def scene_cancel_invite(self, kind):
        """Trade 0x2BC4 / rental 0x2C28 (bare): withdraws every invitation made."""
        ids = SCENE_IDS[kind]
        me = self.selected_player
        invites = STATE.scene_invites[kind]
        for invitee, inviter in list(invites.items()):
            if inviter == me:
                del invites[invitee]
                STATE.online_push(invitee, ids["cancel_push"])
        await self.send_message(ids["cancel_ok"])

    async def scene_answer(self, kind, fields):
        """Trade 0x2BC2 / rental 0x2C26 {to_id: the inviter, answer: 2 yes / 1 no}.

        A yes opens the scene on both; the inviter gets {answer} (2 opens
        its scene). If either side sits in a scene by now the answer reaches
        the inviter as a "no" and the answerer gets {error_code 106}.
        """
        ids = SCENE_IDS[kind]
        me = self.selected_player
        inviter = as_int(fields.get("to_id"))
        answer = as_int(fields.get("answer"), 1)
        invites = STATE.scene_invites[kind]

        if invites.get(me) != inviter:
            await self.send_message(ids["answer_err"], {"error_code": 106})
            return
        del invites[me]

        if answer == 2 and (
            me in STATE.scenes
            or inviter in STATE.scenes
            or inviter not in STATE.online
        ):
            STATE.online_push(inviter, ids["answer_push"], {"answer": 1})
            await self.send_message(ids["answer_err"], {"error_code": 106})
            return
        if answer == 2:
            new_scene(kind, inviter, me)
        await self.send_message(ids["answer_ok"])
        STATE.online_push(inviter, ids["answer_push"], {"answer": 2 if answer == 2 else 1})

    async def scene_ready(self, kind):
        """Trade 0x2BD5 / rental 0x2C3E: this client's scene is open. Once both
        are, both get the start push. No scene: the error id."""
        ids = SCENE_IDS[kind]
        scene = self.active_scene(kind)
        if scene is None:
            await self.send_message(ids["ready_err"], {"error_code": 700})
            return
        scene["ready"].add(self.selected_player)
        await self.send_message(ids["ready_ok"])
        if len(scene["ready"]) == 2 and not scene["started"]:
            scene["started"] = True
            for player_id in scene["players"]:
                STATE.online_push(player_id, ids["start_push"])

    async def scene_leave(self, kind):
        """Trade 0x2BCB / rental 0x2C33: leaves the scene; the partner is told."""
        ids = SCENE_IDS[kind]
        scene = self.active_scene(kind)
        await self.send_message(ids["leave_ok"])
        if scene is not None:
            scene_end(scene, notify=scene_partner(scene, self.selected_player))

    # ------------------------------------------------------------------
    # Card trade (CardTrade.cpp, handler 0x4248F0)
    # ------------------------------------------------------------------

    async def trade_offer(self, fields):
        """0x2BC6 {main_list} -> 0x2BE1; the partner gets 0x2BCF {main_list}.
        0x2BE2 {error_code}: 502 no scene, 504 more cards than are owned.
        Changing an offer withdraws both Ready marks and final answers."""
        scene = self.active_scene("trade")
        me = self.selected_player
        if scene is None:
            await self.send_message(0x2BE2, {"error_code": 502})
            return
        rows = rows_of(fields.get("main_list"))
        if len(rows) > TRADE_OFFER_MAX or Counter(rows) - tradable_counts(me):
            await self.send_message(0x2BE2, {"error_code": 504})
            return
        scene["offers"][me] = rows
        scene["ok"].clear()
        scene["final"].clear()
        await self.send_message(0x2BE1)
        STATE.online_push(
            scene_partner(scene, me), 0x2BCF, {"main_list": pack_rows(rows)}
        )

    async def trade_seen(self):
        """0x2BC7: this scene has the partner's offer -> 0x2BE3; the offerer
        gets 0x2BCE and 0x2BD0 (the offer arrived)."""
        scene = self.active_scene("trade")
        if scene is None:
            await self.send_message(0x2BE4, {"error_code": 502})
            return
        await self.send_message(0x2BE3)
        partner = scene_partner(scene, self.selected_player)
        STATE.online_push(partner, 0x2BCE)
        STATE.online_push(partner, 0x2BD0)

    async def trade_status(self, fields):
        """0x2BC8 {status: 1 Ready to Trade on, 0 off} -> 0x2BE5; the partner gets
        0x2BD1 {status}; with both on, both get 0x2BD2 ("Execute Trade?")."""
        scene = self.active_scene("trade")
        me = self.selected_player
        if scene is None:
            await self.send_message(0x2BE6, {"error_code": 502})
            return
        status = 1 if as_int(fields.get("status")) == 1 else 0
        if status:
            scene["ok"].add(me)
        else:
            scene["ok"].discard(me)
            scene["final"].clear()
        await self.send_message(0x2BE5)
        STATE.online_push(scene_partner(scene, me), 0x2BD1, {"status": status})
        if len(scene["ok"]) == 2:
            for player_id in scene["players"]:
                STATE.online_push(player_id, 0x2BD2)

    async def trade_confirm(self, fields):
        """0x2BC9 {answer: 2 yes / 1 no} -> 0x2BE7 (0x2BE8 {503} before both
        Ready marks are on). Both yes: the cards change hands and both get
        0x2BCA {error_code 0, trade_result 1} and a new trunk. A no, or an
        offer that no longer holds, ends the trade unchanged: 0x2BCA with a
        non-zero error_code (the scene shows "Trade Cancelled" and closes)."""
        scene = self.active_scene("trade")
        me = self.selected_player
        if scene is None or len(scene["ok"]) < 2:
            await self.send_message(0x2BE8, {"error_code": 503})
            return
        answer = 2 if as_int(fields.get("answer")) == 2 else 1
        scene["final"][me] = answer
        await self.send_message(0x2BE7)

        first, second = scene["players"]
        if answer == 1:
            outcome = (503, 0)
        elif len(scene["final"]) < 2 or 1 in scene["final"].values():
            return  # waiting for the other answer
        elif execute_trade(first, scene["offers"][first], second, scene["offers"][second]):
            outcome = (0, 1)
        else:
            outcome = (504, 0)

        for player_id in (first, second):
            STATE.online_push(
                player_id,
                0x2BCA,
                {
                    "error_code": outcome[0],
                    "own_status": 1,
                    "other_status": 1,
                    "trade_result": outcome[1],
                },
            )
        scene_end(scene)
        if outcome[1]:
            for player_id in (first, second):
                session = STATE.online.get(player_id)
                if session is not None:
                    asyncio.create_task(session.send_collection())

    # ------------------------------------------------------------------
    # Card rental (CardRental.cpp, handler 0x41C7E0)
    # The inviter is the lender, the one who accepts the borrower.
    # ------------------------------------------------------------------

    def rental_scene_for(self, lender_only=False):
        """The player's rental scene, or None (also for a borrower when lender_only)."""
        scene = self.active_scene("rental")
        if scene is None or (lender_only and scene["players"][0] != self.selected_player):
            return None
        return scene

    async def rental_conditions(self, fields):
        """0x2C2B {type 0 Free / 1 Deck, days 1, 3, 7 or 14} (the lender) -> 0x2D5C
        (0x2D5D {error_code}); the borrower gets 0x2C39 {type, days}."""
        scene = self.rental_scene_for(lender_only=True)
        kind, days = as_int(fields.get("type"), -1), as_int(fields.get("days"), -1)
        if scene is None or kind not in (0, 1) or days not in RENTAL_DAYS:
            await self.send_message(0x2D5D, {"error_code": 700})
            return
        scene.update(type=kind, days=days, confirmed=False)
        await self.send_message(0x2D5C)
        STATE.online_push(
            scene["players"][1], 0x2C39, {"type": kind, "days": days}
        )

    async def rental_cards(self, fields):
        """0x2C2A {main_list, extra_list, side_list} (the lender) -> 0x2D5A; the
        borrower gets 0x2C38 with the lists. 0x2D5B {error_code}: 700 not the
        lender, 504 cards not owned (or already lent) or a list too long
        (Free: 90 cards in all; Deck: 60 main, 15 extra, 15 side)."""
        scene = self.rental_scene_for(lender_only=True)
        if scene is None:
            await self.send_message(0x2D5B, {"error_code": 700})
            return
        main, extra, side = (
            rows_of(fields.get(name)) for name in ("main_list", "extra_list", "side_list")
        )
        if scene["type"] == 0:
            too_long = len(main) + len(extra) + len(side) > RENTAL_FREE_MAX
        else:
            too_long = (
                len(main) > RENTAL_DECK_MAIN_MAX
                or len(extra) > RENTAL_DECK_SIDE_MAX
                or len(side) > RENTAL_DECK_SIDE_MAX
            )
        wanted = Counter(main) + Counter(extra) + Counter(side)
        if too_long or wanted - tradable_counts(self.selected_player):
            await self.send_message(0x2D5B, {"error_code": 504})
            return
        scene.update(
            main=pack_rows(main), extra=pack_rows(extra), side=pack_rows(side), confirmed=False
        )
        await self.send_message(0x2D5A)
        STATE.online_push(
            scene["players"][1],
            0x2C38,
            {"main_list": scene["main"], "extra_list": scene["extra"], "side_list": scene["side"]},
        )

    async def rental_seen(self):
        """0x2C2C: this scene has the partner's cards or conditions -> 0x2D5E;
        the partner gets 0x2C3A (unlocks the lender's next send)."""
        scene = self.rental_scene_for()
        if scene is None:
            await self.send_message(0x2D5F, {"error_code": 700})
            return
        await self.send_message(0x2D5E)
        STATE.online_push(scene_partner(scene, self.selected_player), 0x2C3A)

    async def rental_confirm(self):
        """0x2C2D (the lender's Confirm) -> 0x2D60 (0x2D61 {error_code}: nothing
        put up); the borrower gets 0x2C2E ("Borrow these cards?")."""
        scene = self.rental_scene_for(lender_only=True)
        if scene is None or not (scene["main"] or scene["extra"] or scene["side"]):
            await self.send_message(0x2D61, {"error_code": 700})
            return
        scene["confirmed"] = True
        await self.send_message(0x2D60)
        STATE.online_push(scene["players"][1], 0x2C2E)

    async def rental_withdraw(self):
        """0x2C31 (the lender takes the Confirm back) -> 0x2D66 (0x2D67 if none
        stands); the borrower gets 0x2C32 and can't answer any more."""
        scene = self.rental_scene_for(lender_only=True)
        if scene is None or not scene["confirmed"]:
            await self.send_message(0x2D67, {"error_code": 700})
            return
        scene["confirmed"] = False
        await self.send_message(0x2D66)
        STATE.online_push(scene["players"][1], 0x2C32)

    async def rental_answer(self, fields):
        """0x2C2F {answer: 2 yes / 1 no} (the borrower) -> 0x2D64 (0x2D65 without
        a standing Confirm). Both get 0x2C30 {trade_result}: 1 "Rental
        Completed." (and the records 0x2D6C), 0 "Rental Cancelled." with the
        scene kept."""
        scene = self.rental_scene_for()
        me = self.selected_player
        if scene is None or scene["players"][1] != me or not scene["confirmed"]:
            await self.send_message(0x2D65, {"error_code": 700})
            return
        await self.send_message(0x2D64)
        lender = scene["players"][0]
        rental = create_rental(lender, me, scene) if as_int(fields.get("answer")) == 2 else None
        result = 1 if rental is not None else 0
        scene["confirmed"] = False
        for player_id in scene["players"]:
            STATE.online_push(player_id, 0x2C30, {"trade_result": result})
        if rental is not None:
            scene_end(scene)
            for player_id in (lender, me):
                push_rentals(player_id)

    async def rental_records(self):
        """0x2C34 (Rental Card List) -> 0x2D6C: the player's running rentals."""
        await self.send_message(
            0x2D6C, items=[rental_wire(r) for r in active_rentals(self.selected_player)]
        )

    async def rental_history(self, fields):
        """0x2C35 {rental_kind 0 lent / 1 borrowed, base_id} -> 0x2D6E: every
        rental of that side, the over ones too (greyed by the client)."""
        me = self.selected_player
        key = "from_id" if as_int(fields.get("rental_kind")) == 0 else "to_id"
        await self.send_message(
            0x2D6E, items=[rental_wire(r) for r in STATE.rentals if r[key] == me]
        )

    # ------------------------------------------------------------------
    # User events (Satellite Plaza): list, Create, entry, lobby, bracket
    # ------------------------------------------------------------------

    async def event_own(self):
        """0x339A (bare) -> 0x33FE {remain, event_id}: the player's own event
        (kept by the scene while remain is non-zero; it turns Create off)."""
        own = uev_hosted_by(self.selected_player)
        await self.send_message(
            0x33FE,
            {"remain": 1 if own else 0, "event_id": own["event_id"] if own else 0},
        )

    async def event_create_form(self):
        """0x339B (bare) -> 0x3400 {create_status}: 0 opens the form; bit 4
        premium needed, 8 already in an event, 1 full."""
        await self.send_message(
            0x3400,
            {"create_status": uev_create_status(self.selected_player, self.account)},
        )

    async def event_create(self, fields):
        """0x3390 (the form, top level) -> 0x33F4 {event_id} (0x33F5 {error_code}).
        The host's own entry follows as 0x3397."""
        if uev_create_status(self.selected_player, self.account):
            await self.send_message(0x33F5, {"error_code": 700})
            return
        event = uev_new(self.selected_player, fields)
        await self.send_message(0x33F4, {"event_id": event["event_id"]})

    async def event_enter(self, fields):
        """0x3397 {event_id} -> 0x33FA (0x33FB {error_code}); the others get
        0x3399 {entry_name, entry_type 1} and the lobby refresh flags."""
        event = STATE.user_events.get(as_int(fields.get("event_id")))
        code = uev_entry_error(event, self.selected_player) if event else 700
        if code:
            await self.send_message(0x33FB, {"error_code": code})
            return
        await self.send_message(0x33FA)
        uev_enter(event, self.selected_player)

    async def event_leave(self, fields):
        """0x178E {event_id} -> 0x178F: leaves an open event (a forfeit in a
        running one); the others get 0x3399 {entry_type 2}."""
        event = STATE.user_events.get(as_int(fields.get("event_id")))
        await self.send_message(0x178F)
        if event is not None and self.selected_player in event["entrants"]:
            uev_player_left(self.selected_player)

    async def event_search(self, fields):
        """0x1789 {search_flag, pname, status, base_point, front_num, ...} ->
        0x177B {result_number} with a row per open or running user event."""
        name = fields.get("pname", "")
        name = name.lower() if isinstance(name, str) else ""
        status = as_int(fields.get("status"))
        base = as_int(fields.get("base_point"))
        front = as_int(fields.get("front_num"), 30) or 30
        events = [
            e
            for _, e in sorted(STATE.user_events.items())
            if (not name or name in e["event_name"].lower())
            and (status not in (USER_EVENT_OPEN, USER_EVENT_RUNNING) or e["status"] == status)
        ]
        await self.send_message(
            0x177B,
            {"result_number": len(events)},
            [uev_row(e, self.selected_player) for e in events[base : base + front]],
        )

    async def event_lobby_info(self, fields):
        """0x177F {event_id} -> 0x1780 item[0]: the row the lobby and the Event
        Menu show (`player_id` is the host, which the client compares with its own)."""
        event = uev_lobby_event(as_int(fields.get("event_id")))
        if event is None:
            await self.send_message(0x1781, {"error_code": 700})
            return
        await self.send_message(0x1780, items=[uev_row(event, self.selected_player)])

    async def event_lobby_query(self, request_id, fields):
        """The lobby's polling requests, each about `event_id`:
        0x32CA entries, 0x32CC timer, 0x32CD counts, 0x32CE phase blocks,
        0x32C9 state, 0x32D5 bracket map, 0x32CB one bracket block."""
        event = uev_lobby_event(as_int(fields.get("event_id")))
        if event is None:
            return
        run = uev_run(event)

        if request_id == 0x32CA:
            start = as_int(fields.get("start_index"))
            items = []
            for player_id in event["entrants"][start : start + 64]:
                session = STATE.online.get(player_id)
                player = STATE.player(player_id) or {}
                items.append(
                    {
                        "player_id": player_id,
                        "player_name": player.get("pname", ""),
                        "remain": 0,
                        "avatar1": player.get("avatar1") or b"\x00" * 82,
                        "country": session.country if session else "GB",
                    }
                )
            await self.send_message(0x32FD, {"total_num": len(event["entrants"])}, items)
        elif request_id == 0x32CC:
            timer = max(0, int(run["deadline"] - time.time() + 0.999)) if run["deadline"] else 0
            await self.send_message(0x3301, {"timer": timer})
        elif request_id == 0x32CD:
            running = run["status"] != 0
            await self.send_message(
                0x3303,
                {
                    "alive_num": len(run["alive"]) if running else len(event["entrants"]),
                    "entry_num": len(event["entrants"]),
                    "watch_num": 0,
                },
            )
        elif request_id == 0x32CE:
            blocks = uev_phase_blocks(event)
            await self.send_message(
                0x3305,
                {
                    "phase_block1": blocks[0],
                    "phase_block2": blocks[1],
                    "phase_block3": blocks[2],
                    "remain": sum(1 for d in run["duels"].values() if d["winner"] is None),
                },
            )
        elif request_id == 0x32C9:
            await self.send_message(0x32FB, uev_state_wire(event))
        elif request_id == 0x32D5:
            await self.send_message(0x3309, {"map_data": uev_map_data(event)})
        elif request_id == 0x32CB:
            item = uev_block_item(event, as_int(fields.get("block_index")))
            await self.send_message(0x32FF, items=[item] if item else [])

    async def event_start(self, fields):
        """0x3395 {event_id} (the host) -> 0x33F8 (0x33F9 {error_code}); everyone
        gets 0x3396 and, pair by pair, the match push 0x1978."""
        event = STATE.user_events.get(as_int(fields.get("event_id")))
        if (
            event is None
            or event["host_id"] != self.selected_player
            or event["status"] != USER_EVENT_OPEN
            or len(event["entrants"]) < 2
        ):
            await self.send_message(0x33F9, {"error_code": 700})
            return
        await self.send_message(0x33F8)
        uev_start(event)

    async def event_cancel(self, fields):
        """0x3393 {event_id} (the host) -> 0x33F6 (0x33F7 {error_code}); the
        others get 0x3394 ("Event cancelled.") and the event is gone."""
        event = STATE.user_events.get(as_int(fields.get("event_id")))
        if event is None or event["host_id"] != self.selected_player:
            await self.send_message(0x33F7, {"error_code": 700})
            return
        await self.send_message(0x33F6)
        uev_cancel(event, skip={self.selected_player})

    async def shop_buy(self, fields):
        """0x1B68 {sale_id, item_num} -> 0x1C24 {dpoint} (0x1C25 {error_code}).

        Everything bought arrives as a present: after 0x1C24 the client asks
        0x2775 until 0x2778. Sale ids: a pack's id, 20000 + a structure deck's
        id, 30000 + a protector's or avatar item's pbin id.
        """
        player = self.player()
        sale_id = as_int(fields.get("sale_id"))
        count = max(1, min(as_int(fields.get("item_num"), 1), 99))

        async def refuse(code=1):
            log.warning("[Shop] Purchase refused: sale_id=%s code=%s", sale_id, code)
            await self.send_message(0x1C25, {"error_code": code})

        if player is None:
            await refuse(700)
            return

        owned_items = player.setdefault("pbin_items", [])
        cards = player.setdefault("cards", {})
        presents = player.setdefault("presents", [])
        new_presents = []
        cost = 0
        granted = []

        deck_match = next(
            (
                d
                for d in getattr(GAME, "structure_decks", [])
                if DECK_SALE + int(d.deck.deck_id) == sale_id
            ),
            None,
        )
        pack = next(
            (p for p in (getattr(GAME, "packs", []) or []) if int(p.id) == sale_id),
            None,
        )
        catalogue_item = None
        if deck_match is None and pack is None:
            catalogue_item = next(
                (
                    i
                    for i in (
                        getattr(GAME, "avatar_items", [])
                        + getattr(GAME, "protector_items", [])
                        + getattr(GAME, "sleeve_items", [])
                        + getattr(GAME, "duel_disk_items", [])
                    )
                    if ITEM_SALE + int(i.item_id) == sale_id
                ),
                None,
            )

        if deck_match is not None:
            count = min(count, DECK_MAX)
            cost = DECK_COST * count
            for _ in range(count):
                granted.extend(deck_match.deck.cards())
                new_presents.append(
                    new_present(
                        1,
                        item=struct.pack(">H", int(deck_match.deck.deck_id)),
                        desc="-1",
                    )
                )
        elif pack is not None:
            count = min(count, PACK_MAX)
            cost = PACK_COST * count
            try:
                for _ in range(count):
                    rows = open_shop_pack(pack)
                    if not rows:
                        raise ValueError("empty pack")
                    granted.extend(rows)
                    new_presents.append(
                        new_present(0, pack_id=int(pack.id), item=pack_rows(rows))
                    )
            except Exception:
                log.exception("[Shop] Failed to generate booster cards")
                await refuse(700)
                return
        elif catalogue_item is not None:
            item_id = int(catalogue_item.item_id)
            if item_id in owned_items:
                await refuse()
                return
            cost = int(catalogue_item.cost)
            is_protector = catalogue_item.item_type == PROTECTOR_TYPE
            new_presents.append(
                new_present(
                    7 if is_protector else 6,
                    name_id=int(getattr(catalogue_item, "name_id", 0) or 0),
                    item=struct.pack(">H", item_id),
                )
            )
            owned_items.append(item_id)
        else:
            await refuse()
            return

        if self.account.duel_points < cost:
            if catalogue_item is not None:
                owned_items.remove(int(catalogue_item.item_id))
            await refuse()
            return

        self.account.duel_points -= cost
        for row in granted:
            if row in GAME.cards:
                cards[row] = cards.get(row, 0) + 1
        presents.extend(new_presents)
        STATE.save()

        log.info(
            "[Shop] Player %s bought sale_id=%s x%d for %d BP",
            self.selected_player,
            sale_id,
            count,
            cost,
        )
        await self.send_message(0x1C24, {"dpoint": self.account.duel_points})

    async def next_present(self):
        """0x2775 (bare) -> 0x2777 with the next present, or 0x2778 for none."""
        player = self.player()
        queue = player.get("presents") if player else None
        if not queue:
            await self.send_message(0x2778)
            return
        present = queue.pop(0)
        STATE.save()

        await self.send_message(0x2777, dict(present), [dict(present)])


YO3_SERVER_DUEL = 0x200D
YO3_CLIENT_DUEL = 0x200C
YO3_ENTER = 0x200A
YO3_ENGINE_COMMAND = 0x11
YO3_Roshambo_BEGIN = 1500
YO3_Roshambo_RESOLVE = 1501
YO3_FIRST_BEGIN = 1502
YO3_FIRST_RESOLVE = 1503
YO3_SELECT_HAND = 0x0C
YO3_SELECT_FIRST = 13
YO3_FIRST = 1
YO3_SECOND = 2


# One connection on the duel port: runs the duel for its player (NPC duels only), translating
# engine messages for the client.
class DuelSession:
    # Binds the stream and the per-duel mirror of the board.
    def __init__(self, reader, writer, turn_timeout: int = 180):
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info("peername")
        self.player_id = None
        self.duel_id = None
        self.closed = False
        self.heartbeats = 0
        self.send_lock = asyncio.Lock()

        self.turn_timeout = turn_timeout
        self.turn_start_time = time.monotonic()
        self.game_start_time = None
        self.timer_task = None

        self.Roshambo_complete = False
        self.turn_order_complete = False
        self.player_hand = None
        self.cpu_hand = None
        self.Roshambo_result = None
        self.turn_order = None
        self.pending_list_indices = []
        self.pending_list_prompt = None
        self.team_to_client = {0: 0, 1: 1}
        self.uid_by_team_code = {0: {}, 1: {}}
        self.extra_pool = {0: {}, 1: {}}
        self.uid_to_code = {}
        self.card_locations = {}
        self._init_mirror()

    # Sends a duel-stream message.
    async def send_message(self, request_id, fields=None, items=None):
        if self.closed:
            return
        fields, items = fields or {}, items or []
        async with self.send_lock:
            self.writer.write(encode_frame(make_reply(request_id, fields, items)))
            await self.writer.drain()
        log.info(
            "[Duel] Sent YO3 packet 0x%04X fields=%r items=%d",
            request_id,
            fields,
            len(items),
        )

    # Sends a client engine command (id + up to four parameters).
    async def send_engine_command(self, command_id, params=None):
        params = list(params or [])
        if len(params) > 4:
            raise ValueError("YO3 engine command takes at most four parameters")
        params += [0] * (4 - len(params))
        fields = {
            "duelRequestName": YO3_ENGINE_COMMAND,
            "id": int(command_id),
            "param1": int(params[0]),
            "param2": int(params[1]),
            "param3": int(params[2]),
            "param4": int(params[3]),
        }
        await self.send_message(YO3_SERVER_DUEL, fields)

    # Restarts the turn clock.
    async def reset_timer(self):
        self.turn_start_time = time.monotonic()

    # Sends the independent turn and match clocks to the client. The match clock
    # is carried forward on every turn update instead of restarting at 10:00.
    async def send_duel_timer(self, timer_kind):
        turn_time = max(1, int(self.turn_timeout))
        if self.game_start_time is None:
            self.game_start_time = time.monotonic()
        elapsed = max(0, int(time.monotonic() - self.game_start_time))
        total_time = max(0, DUEL_TOTAL_TIME - elapsed)
        fields = {
            "duelRequestName": 0x14,
            "timerKind": int(timer_kind),
            "start": 1,
            "count1": turn_time,
            "total1": total_time,
            "count2": turn_time,
            "total2": total_time,
            "other": 0,
            "count1_max": turn_time,
            "total1_max": total_time,
            "count2_max": turn_time,
            "total2_max": total_time,
            "other_max": 0,
        }
        await self.send_message(YO3_SERVER_DUEL, fields)

    # Starts the rock-paper-scissors that decides who chooses the turn order.
    async def begin_Roshambo(self):
        log.info("[Duel] Sending Roshambo_BEGIN to player %s", self.player_id)
        await self.send_engine_command(YO3_Roshambo_BEGIN, [0, 0, 0, 0])

    async def send_opening_setup(self, duel, order):
        """Build the YO3 opening board from the already-started OCG duel."""
        rows_by_team = duel["original_deck_rows"]
        decks_by_team = duel["decks"]

        npc_seat = duel.get("npc_seat")
        if npc_seat not in (0, 1):
            raise RuntimeError("Cannot determine NPC seat for duel setup")

        client_team = 1 - npc_seat
        cpu_team = 1 - client_team
        team_to_client = {client_team: 0, cpu_team: 1}

        first = 0 if order == YO3_FIRST else 1

        team_cards = {}
        for team in (0, 1):
            original = rows_by_team[team]
            converted = decks_by_team[team]

            main = list(zip(original["main"], converted["main"]))
            extra = list(zip(original["extra"], converted["extra"]))

            if len(main) != len(converted["main"]):
                raise RuntimeError("Main Deck row/passcode count mismatch")
            if len(extra) != len(converted["extra"]):
                raise RuntimeError("Extra Deck row/passcode count mismatch")

            team_cards[team] = {
                "main": main,
                "extra": extra,
            }

        parsed = duel.get("initial_parsed_messages", [])

        cut = next(
            (i for i, m in enumerate(parsed) if m.get("type") == MSG["NEW_TURN"]),
            len(parsed),
        )
        parsed = parsed[:cut]
        drawn_by_team = {0: [], 1: []}

        for msg in parsed:
            if msg.get("type") != MSG["DRAW"]:
                continue
            team = int(msg.get("player", 0))
            if team not in drawn_by_team:
                continue
            for card in msg.get("drawn", []):
                drawn_by_team[team].append(int(card["code"]))

        for team in (0, 1):
            remaining = list(team_cards[team]["main"])
            ordered = []

            for code in drawn_by_team[team]:
                match_index = next(
                    (
                        i
                        for i, (_, card_code) in enumerate(remaining)
                        if card_code == code
                    ),
                    None,
                )
                if match_index is None:
                    raise RuntimeError(
                        f"Cannot map drawn OCG card {code} to YO3 deck row"
                    )
                ordered.append(remaining.pop(match_index))

            team_cards[team]["main"] = ordered + remaining

        client_cards = team_cards[client_team]
        cpu_cards = team_cards[cpu_team]

        await self.send_message(
            YO3_SERVER_DUEL,
            {
                "duelRequestName": 0x15,
                "FirstPlayer": first,
                "PlayerNum": 0,
                "PlayerType001": 0,
                "PlayerType002": 1,
                "MainDeckNum001": len(client_cards["main"]),
                "MainDeckNum002": len(cpu_cards["main"]),
                "ExtraDeckNum001": len(client_cards["extra"]),
                "ExtraDeckNum002": len(cpu_cards["extra"]),
            },
        )

        uid_by_team_code = {0: {}, 1: {}}
        extra_pool = {0: {}, 1: {}}
        next_uid = 1

        for team in (client_team, cpu_team):
            yo3_player = team_to_client[team]

            for index, (row, code) in enumerate(team_cards[team]["main"]):
                uid = next_uid
                next_uid += 1

                uid_by_team_code[team].setdefault(code, []).append((uid, row))

                table_place = yo3_player | (0x0D << 8) | (index << 16)
                await self.send_engine_command(
                    1000, [uid, row if yo3_player == 0 else 0, 0, table_place]
                )

            for index, (row, code) in enumerate(team_cards[team]["extra"]):
                uid = next_uid
                next_uid += 1

                extra_pool[team].setdefault(code, []).append((uid, row))
                table_place = yo3_player | (0x0C << 8) | (index << 16)
                await self.send_engine_command(
                    1000, [uid, row if yo3_player == 0 else 0, 0, table_place]
                )

        self.team_to_client = dict(team_to_client)
        self.uid_by_team_code = {
            team: {code: list(entries) for code, entries in by_code.items()}
            for team, by_code in uid_by_team_code.items()
        }
        self.card_locations = {}
        self.uid_to_code = {}
        self._init_mirror()
        for team in (client_team, cpu_team):
            player = team_to_client[team]
            for zone, cards in (
                (0x0D, team_cards[team]["main"]),
                (0x0C, team_cards[team]["extra"]),
            ):
                for index, (row, code) in enumerate(cards):
                    candidates = self.uid_by_team_code[team].get(code, [])
                    if candidates:
                        self.card_locations[(team, zone, index)] = candidates.pop(0)
                        self.uid_to_code[
                            self.card_locations[(team, zone, index)][0]
                        ] = code

        await self.send_engine_command(1, [0, 0, 0, 0])
        await self.send_engine_command(0x0F, [0, 8000, 0, 0])
        await self.send_engine_command(0x0F, [1, 8000, 0, 0])

        for player, zone in (
            (0, 0x0D),
            (1, 0x0D),
            (0, 0x0C),
            (1, 0x0C),
        ):
            await self.send_engine_command(0x16, [player, zone, 0, 0])

        hand_index = {0: 0, 1: 0}

        for msg in parsed:
            if msg.get("type") != MSG["DRAW"]:
                continue

            team = int(msg.get("player", 0))
            if team not in team_to_client:
                continue

            player = team_to_client[team]

            for card in msg.get("drawn", []):
                code = int(card["code"])
                candidates = uid_by_team_code[team].get(code, [])

                if not candidates:
                    raise RuntimeError(
                        f"No registered YO3 card for drawn OCG code {code}"
                    )

                uid, row = candidates.pop(0)

                for old_key, old_value in list(self.card_locations.items()):
                    if old_key[0] == team and old_value[0] == uid:
                        self.card_locations.pop(old_key, None)
                        break
                index = hand_index[player]
                hand_index[player] += 1
                self.card_locations[(team, 0x0B, index)] = (uid, row)
                self.uid_to_code[uid] = code
                self.m_hand[team].append((uid, row))

                hand_place = player | (0x0B << 8) | (index << 16)
                await self.send_engine_command(
                    1000, [uid, row if player == 0 else 0, 0, hand_place]
                )

                from_place = player | (0x0D << 1)
                to_place = player | (0x0B << 1) | (index << 6)

                await self.send_engine_command(
                    0x1A,
                    [uid | (row << 16), from_place, to_place, 0],
                )

        self.uid_by_team_code = uid_by_team_code
        self.extra_pool = extra_pool
        for pool in (uid_by_team_code, extra_pool):
            for by_code in pool.values():
                for code, entries in by_code.items():
                    for uid, _row in entries:
                        self.uid_to_code[uid] = code
        self.skip_draw_phase = True
        self.turn_count = 0

        await self.send_engine_command(5, [first, 0, 0, 0])

        log.info(
            "[Duel] YO3 opening setup sent: first=%s hands=%s",
            first,
            hand_index,
        )

    async def _reseat(self, duel_id, duel):
        """The engine's first turn always belongs to team 0, so when the janken
        (or the choice) puts the CPU first the duel is rebuilt with the seats
        swapped and the CPU's deck on team 0."""
        npc_id = duel.get("npc_player_id")
        p1, p2 = duel["players"]
        d1, d2 = duel["original_decks"]
        DUEL_MANAGER.remove_duel(duel_id)
        new_id = DUEL_MANAGER.create_duel(p2, p1, d2, d1, npc_player_id=npc_id)
        new_duel = DUEL_MANAGER.get_duel(new_id)
        DUEL_CONNECTIONS.pop((duel_id, self.player_id), None)
        DUEL_CONNECTIONS[(new_id, self.player_id)] = self
        self.duel_id = new_id
        await self.process_initial_core_state(new_id, new_duel)
        log.info("[Duel] Re-seated duel %s as %s (CPU goes first)", duel_id, new_id)
        return new_id, new_duel

    # Applies the winner's first/second choice and starts the duel.
    async def resolve_turn_order(self, order):
        if order not in (YO3_FIRST, YO3_SECOND) or self.turn_order_complete:
            return

        found = DUEL_MANAGER.find_duel_for_player(self.player_id)
        if found is None:
            raise RuntimeError("Duel disappeared before opening setup")

        duel_id, duel = found

        self.turn_order = order
        self.turn_order_complete = True

        want_client_team = 0 if order == YO3_FIRST else 1
        if 1 - int(duel.get("npc_seat", 1)) != want_client_team:
            duel_id, duel = await self._reseat(duel_id, duel)

        self.game_start_time = time.monotonic()
        await self.send_duel_timer(0 if order == YO3_FIRST else 1)
        await self.send_engine_command(YO3_FIRST_RESOLVE, [order, 0, 0, 0])

        await self.send_opening_setup(duel, order)

        initial = duel.get("initial_parsed_messages", [])
        cut = next(
            (i for i, m in enumerate(initial) if m.get("type") == MSG["NEW_TURN"]),
            len(initial),
        )
        rest = initial[cut:]
        if not rest and duel.get("pending_prompt"):
            rest = [duel["pending_prompt"]]
        await self._pump(
            duel_id, duel, rest, duel.get("ocg_status", OCG_DUEL_STATUS_AWAITING)
        )

    # Plays the player's hand against the CPU's; repeats on a draw.
    async def resolve_Roshambo(self, player_hand):
        if player_hand not in (0, 1, 2):
            return
        self.player_hand = player_hand
        self.cpu_hand = secrets.randbelow(3)
        result = (
            0
            if self.cpu_hand == player_hand
            else (1 if self.cpu_hand == (player_hand + 1) % 3 else 2)
        )
        self.Roshambo_result = result

        response_payload = resp_rps(result)

        await self.send_engine_command(
            YO3_Roshambo_RESOLVE, [result, self.cpu_hand, player_hand, 0]
        )
        if result == 0:
            return
        self.Roshambo_complete = True
        if result == 1:
            await self.send_engine_command(YO3_FIRST_BEGIN, [1, 0, 0, 0])
        else:
            await self.send_engine_command(YO3_FIRST_BEGIN, [0, 0, 0, 0])
            await self.resolve_turn_order(YO3_SECOND)

    # Reads the engine's first output into the opening state.
    async def process_initial_core_state(self, duel_id, duel):
        status = duel.get("ocg_status")
        messages = duel.get("ocg_messages", b"")
        parsed_msgs = []

        if messages:
            parsed_msgs = parse_messages(messages, strict=False)
            log.info(
                "[Duel] Initial OCG messages: %s",
                [MSG_NAME.get(msg["type"], msg["type"]) for msg in parsed_msgs],
            )

            for msg in parsed_msgs:
                if msg["type"] == MSG["WIN"]:
                    log.info(
                        "[Duel] Win condition detected: player=%s reason=%s",
                        msg.get("player"),
                        msg.get("reason"),
                    )

        duel["initial_parsed_messages"] = parsed_msgs
        duel["initial_processed"] = True
        for event in parsed_msgs:
            if event.get("type") in PROMPTS:
                duel["pending_prompt"] = event

        log.info(
            "[Duel] Initial state saved: duel=%s status=%s messages=%d",
            duel_id,
            status,
            len(parsed_msgs),
        )

        native_duel = duel.get("native")
        if native_duel and DUEL_ENGINE:
            monsters = field_monsters(DUEL_ENGINE.dll, native_duel.handle)
            log.info("[Duel] Initial field monsters tracked: %d active", len(monsters))

    # Resets the server's copy of the client's card table.
    def _init_mirror(self):
        self.m_hand = {0: [], 1: []}
        self.m_field = {}
        self.m_pile = {}
        self.turn_team = None
        self.turn_count = 0
        self.phase_index = None
        self.in_battle = False
        self.skip_draw_phase = False
        self.duel_ended = False
        self.next_token_uid = 0x4000
        self.pending_response_kind = None
        self.pending_list_indices = []
        self.pending_list_prompt = None

    # Maps an engine player (team) to the client's player number.
    def _cp(self, team):
        return self.team_to_client.get(int(team), int(team))

    # Maps a client player to the engine team.
    def _team(self, player):
        for team, client in self.team_to_client.items():
            if client == int(player):
                return team
        return int(player)

    # The engine team the human plays.
    @property
    def _client_team(self):
        return self._team(0)

    @staticmethod
    def _zone(location, sequence=0):
        """The client's zone number: 0-4 monsters, 5-9 Spell/Trap, 10 Field Spell,
        0xB hand, 0xC extra deck, 0xD deck, 0xE graveyard, 0xF banished."""
        location, sequence = int(location), int(sequence)
        if location == LOC_MZONE:
            return min(sequence, 4)
        if location == LOC_SZONE:
            if sequence == 5:
                return 10
            return 5 + min(sequence - 6 if sequence >= 6 else sequence, 4)
        return {
            LOC_HAND: 0x0B,
            LOC_EXTRA: 0x0C,
            LOC_DECK: 0x0D,
            LOC_GRAVE: 0x0E,
            LOC_REMOVED: 0x0F,
        }.get(location, 0)

    @staticmethod
    def _place_word(player, zone, index=0, position=0):
        """A move place: player | zone<<1 | index<<6, with the face-up bit 14 and
        (monsters only) the defence bit 15."""
        value = int(player) | (int(zone) << 1) | (int(index) << 6)
        faceup = position & (POS_FACEUP_ATTACK | POS_FACEUP_DEFENSE)
        if zone <= 10:
            if faceup:
                value |= 1 << 14
            if zone <= 4 and position & (POS_FACEUP_DEFENSE | POS_FACEDOWN_DEFENSE):
                value |= 1 << 15
        elif zone == 0x0E or (zone == 0x0F and faceup):
            value |= 1 << 14
        return value

    # The client card-table key for a zone and index.
    @staticmethod
    def _table_place(player, zone, index=0):
        return int(player) | (int(zone) << 8) | (int(index) << 16)

    def _visible_row(self, team, zone, position, row):
        """The CPU's hidden cards go to the client with row 0."""
        if self._cp(team) == 1 and (
            zone in (0x0B, 0x0C, 0x0D)
            or (zone <= 10 and position in (POS_FACEDOWN_ATTACK, POS_FACEDOWN_DEFENSE))
        ):
            return 0
        return row

    def _take(self, team, location, sequence, code):
        """Remove a card from the mirror: (uid, row, position, index) or None."""
        code = int(code)
        if location == LOC_HAND:
            hand = self.m_hand[team]
            idx = (
                sequence
                if 0 <= sequence < len(hand)
                and self.uid_to_code.get(hand[sequence][0]) == code
                else next(
                    (
                        i
                        for i, (u, _r) in enumerate(hand)
                        if self.uid_to_code.get(u) == code
                    ),
                    None,
                )
            )
            if idx is None:
                return None
            uid, row = hand.pop(idx)
            return uid, row, 0, idx
        if location in (LOC_MZONE, LOC_SZONE):
            entry = self.m_field.pop((team, location, sequence), None)
            if entry is None:
                key = next(
                    (
                        k
                        for k, v in self.m_field.items()
                        if k[0] == team
                        and k[1] == location
                        and self.uid_to_code.get(v[0]) == code
                    ),
                    None,
                )
                if key is None:
                    return None
                entry = self.m_field.pop(key)
            return entry[0], entry[1], entry[2], 0
        if location in (LOC_GRAVE, LOC_REMOVED):
            pile = self.m_pile.setdefault((team, location), [])
            idx = (
                sequence
                if 0 <= sequence < len(pile)
                and self.uid_to_code.get(pile[sequence][0]) == code
                else next(
                    (
                        i
                        for i, (u, _r) in enumerate(pile)
                        if self.uid_to_code.get(u) == code
                    ),
                    None,
                )
            )
            if idx is None:
                return None
            uid, row = pile.pop(idx)
            return uid, row, POS_FACEUP_ATTACK, idx
        if location in (LOC_DECK, LOC_EXTRA):
            for pool in (
                (self.uid_by_team_code, self.extra_pool)
                if location == LOC_DECK
                else (self.extra_pool, self.uid_by_team_code)
            ):
                entries = pool.get(team, {}).get(code)
                if entries:
                    uid, row = entries.pop(0)
                    return uid, row, POS_FACEDOWN_DEFENSE, 0
        return None

    def _put(self, team, location, sequence, uid, row, position):
        """Add a card to the mirror; returns its index in a hand or pile."""
        if location == LOC_HAND:
            self.m_hand[team].append((uid, row))
            return len(self.m_hand[team]) - 1
        if location in (LOC_MZONE, LOC_SZONE):
            self.m_field[(team, location, sequence)] = (uid, row, position)
            return 0
        if location in (LOC_GRAVE, LOC_REMOVED):
            pile = self.m_pile.setdefault((team, location), [])
            pile.append((uid, row))
            return len(pile) - 1
        code = self.uid_to_code.get(uid, 0)
        pool = self.uid_by_team_code if location == LOC_DECK else self.extra_pool
        pool.setdefault(team, {}).setdefault(code, []).insert(0, (uid, row))
        return 0

    def _lookup(self, team, location, sequence, code):
        """(uid, row, position) of an OCG card, or None, without removing it."""
        team, location, sequence, code = (
            int(team),
            int(location),
            int(sequence),
            int(code),
        )
        if location == LOC_HAND:
            hand = self.m_hand.get(team, [])
            if 0 <= sequence < len(hand):
                return hand[sequence][0], hand[sequence][1], 0
        elif location in (LOC_MZONE, LOC_SZONE):
            entry = self.m_field.get((team, location, sequence))
            if entry:
                return entry
        elif location in (LOC_GRAVE, LOC_REMOVED):
            pile = self.m_pile.get((team, location), [])
            if 0 <= sequence < len(pile):
                return pile[sequence][0], pile[sequence][1], POS_FACEUP_ATTACK
        for pool in (self.uid_by_team_code, self.extra_pool):
            entries = pool.get(team, {}).get(code)
            if entries:
                return entries[0][0], entries[0][1], POS_FACEDOWN_DEFENSE
        return None

    # Re-sends hand positions after cards left or arrived.
    async def _hand_refresh(self, team, start):
        player = self._cp(team)
        for i in range(start, len(self.m_hand[team])):
            uid, row = self.m_hand[team][i]
            await self.send_engine_command(
                1000,
                [
                    uid,
                    self._visible_row(team, 0x0B, 0, row),
                    0,
                    self._table_place(player, 0x0B, i),
                ],
            )

    # Re-sends a deck/grave/banished pile's order.
    async def _pile_refresh(self, team, location, start):
        player = self._cp(team)
        zone = 0x0E if location == LOC_GRAVE else 0x0F
        pile = self.m_pile.get((team, location), [])
        for i in range(start, len(pile)):
            uid, row = pile[i]
            await self.send_engine_command(
                1000, [uid | (1 << 24), row, 0, self._table_place(player, zone, i)]
            )

    async def _send_monster_record(self, player, zone, row):
        """The 9-value record behind a monster's label and card info panel."""
        card = GAME.cards.get(row)
        if card is None:
            return

        def stat(value):
            return 0xFFFF if value < 0 or value >= 5110 else int(value)

        key = int(player) | (int(zone) << 8)
        await self.send_engine_command(0x3E9, [0, card.cid, card.race, card.attribute])
        await self.send_engine_command(
            0x3EA, [0, card.level, stat(card.atk), stat(card.defense)]
        )
        await self.send_engine_command(
            0x3EB, [key, stat(card.atk), stat(card.defense), card.cid]
        )

    # Mirrors an engine MSG_MOVE to the client's card table.
    async def _on_move(self, msg):
        src, dst = msg.get("from", {}), msg.get("to", {})
        code = int(msg.get("card", 0))
        s_team, s_loc = int(src.get("controller", 0)), int(src.get("location", 0))
        s_seq = int(src.get("sequence", 0))
        d_team, d_loc = int(dst.get("controller", 0)), int(dst.get("location", 0))
        d_seq, d_pos = int(dst.get("sequence", 0)), int(dst.get("position", 0))

        if d_loc & LOC_OVERLAY or s_loc & LOC_OVERLAY:
            log.warning("[Duel] Xyz material moves are not translated (code=%s)", code)
            return

        if s_loc == 0:

            uid = self.next_token_uid
            self.next_token_uid += 1
            row = 3902
            self.uid_to_code[uid] = code
            taken = (uid, row, d_pos, 0)
            s_zone_hint = None
        else:
            taken = self._take(s_team, s_loc, s_seq, code)
            s_zone_hint = True
            if taken is None:
                log.warning(
                    "[Duel] MOVE of a card the mirror does not hold: code=%s from=%s",
                    code,
                    src,
                )
                return

        uid, row, old_pos, s_idx = taken
        s_player, d_player = self._cp(s_team), self._cp(d_team)

        if d_loc == 0:
            s_zone = self._zone(s_loc, s_seq)
            await self.send_engine_command(0x1F, [s_player, s_zone, 0, 0])
            return

        s_zone = self._zone(s_loc, s_seq) if s_zone_hint else self._zone(d_loc, d_seq)
        d_zone = self._zone(d_loc, d_seq)
        s_index = s_idx if s_zone in (0x0B, 0x0E, 0x0F) else 0
        s_position = int(src.get("position", old_pos)) if s_loc else d_pos

        if s_loc in (LOC_GRAVE, LOC_REMOVED):
            await self._pile_refresh(s_team, s_loc, s_idx)

        d_idx = self._put(d_team, d_loc, d_seq, uid, row, d_pos)
        d_index = d_idx if d_zone in (0x0B, 0x0E, 0x0F) else 0

        flag = (1 << 24) if d_zone in (0x0E, 0x0F) else 0
        await self.send_engine_command(
            1000,
            [
                uid | flag,
                self._visible_row(d_team, d_zone, d_pos, row),
                0,
                self._table_place(d_player, d_zone, d_index),
            ],
        )
        await self.send_engine_command(
            0x1A,
            [
                uid | (row << 16),
                self._place_word(s_player, s_zone, s_index, s_position),
                self._place_word(d_player, d_zone, d_index, d_pos),
                0,
            ],
        )

        if s_loc == LOC_HAND:
            await self._hand_refresh(s_team, s_idx)

        if d_loc == LOC_MZONE and d_pos & (POS_FACEUP_ATTACK | POS_FACEUP_DEFENSE):
            await self._send_monster_record(d_player, d_zone, row)

    # Mirrors a position change (attack/defense, face-up/down).
    async def _on_position(self, team, location, sequence, code, new_position):
        key = (team, location, sequence)
        entry = self.m_field.get(key)
        if entry is None:
            key = next(
                (
                    k
                    for k, v in self.m_field.items()
                    if k[0] == team
                    and k[1] == location
                    and self.uid_to_code.get(v[0]) == int(code)
                ),
                None,
            )
            entry = self.m_field.get(key) if key else None
        if entry is None:
            return
        uid, row, old_position = entry
        self.m_field[key] = (uid, row, new_position)
        player = self._cp(team)
        zone = self._zone(location, key[2])
        old_word = self._place_word(player, zone, 0, old_position)
        new_word = self._place_word(player, zone, 0, new_position)

        await self.send_engine_command(0x1A, [uid | (row << 16), old_word, new_word, 0])
        await self.send_engine_command(0x1C, [new_word, 0, 0, 0])
        if location == LOC_MZONE and new_position & (
            POS_FACEUP_ATTACK | POS_FACEUP_DEFENSE
        ):
            await self._send_monster_record(player, zone, row)

    # Translates one engine event to client commands (summons, attacks, damage, chains...).
    async def _translate_event(self, msg):
        t = msg.get("type")
        if t == MSG["NEW_TURN"]:
            team = int(msg.get("player", 0))
            self.turn_team = team
            self.phase_index = None
            await self.reset_timer()
            await self.send_duel_timer(self._cp(team))

            if self.turn_count > 0:
                await self.send_engine_command(6, [self._cp(team), 0, 0, 0])
            self.turn_count += 1
        elif t == MSG["NEW_PHASE"]:
            phase = int(msg.get("phase", 0))
            index = (
                0
                if phase == 0x01
                else (
                    1
                    if phase == 0x02
                    else (
                        2
                        if phase == 0x04
                        else (
                            3
                            if phase in (0x08, 0x10, 0x20, 0x40, 0x80)
                            else 4 if phase == 0x100 else 5
                        )
                    )
                )
            )
            if self.in_battle and index != 3:
                self.in_battle = False
                await self.send_engine_command(0xE, [0, 0, 0, 0])
            if index == 3:
                self.in_battle = True
            if self.skip_draw_phase and index == 0:
                self.skip_draw_phase = False
                self.phase_index = 0
            elif index != self.phase_index:
                self.phase_index = index
                team = self.turn_team if self.turn_team is not None else 0
                await self.send_engine_command(5, [self._cp(team), index, 0, 0])
        elif t == MSG["DRAW"]:
            team = int(msg.get("player", 0))
            player = self._cp(team)
            for card in msg.get("drawn", []):
                code = int(card["code"])
                entries = self.uid_by_team_code.get(team, {}).get(code)
                if not entries:
                    log.warning(
                        "[Duel] Drawn card %s is not in the mirror's deck (team %s)",
                        code,
                        team,
                    )
                    continue
                uid, row = entries.pop(0)
                self.m_hand[team].append((uid, row))
                index = len(self.m_hand[team]) - 1
                await self.send_engine_command(
                    1000,
                    [
                        uid,
                        self._visible_row(team, 0x0B, 0, row),
                        0,
                        self._table_place(player, 0x0B, index),
                    ],
                )
                await self.send_engine_command(
                    0x1A,
                    [
                        uid | (row << 16),
                        self._place_word(player, 0x0D),
                        self._place_word(player, 0x0B, index),
                        0,
                    ],
                )
        elif t == MSG["MOVE"]:
            await self._on_move(msg)
        elif t in (MSG["SUMMONING"], MSG["SPSUMMONING"]):
            team, seq = int(msg.get("controller", 0)), int(msg.get("sequence", 0))
            found = self._lookup(team, LOC_MZONE, seq, msg.get("code", 0))
            if found:
                player = self._cp(team)
                if t == MSG["SUMMONING"]:
                    await self.send_engine_command(
                        0x38,
                        [player | (self._zone(LOC_MZONE, seq) << 1), found[0], 0, 0],
                    )
                else:
                    await self.send_engine_command(0x39, [player, found[0], 0, 0])
        elif t == MSG["POS_CHANGE"]:
            await self._on_position(
                int(msg.get("controller", 0)),
                int(msg.get("location", LOC_MZONE)),
                int(msg.get("sequence", 0)),
                msg.get("code", 0),
                int(msg.get("position", 0)),
            )
        elif t == MSG["FLIPSUMMONING"]:
            await self._on_position(
                int(msg.get("controller", 0)),
                LOC_MZONE,
                int(msg.get("sequence", 0)),
                msg.get("code", 0),
                POS_FACEUP_ATTACK,
            )
        elif t in (MSG["DAMAGE"], MSG["RECOVER"], MSG["PAY_LPCOST"]):
            player = int(msg.get("player", 0))
            amount = int(msg.get("amount", 0))

            delta = amount if t == MSG["RECOVER"] else -amount

            # param3 stays 0: a non-zero value opens an LP pop-up on the client that
            # never closes (the LP text stuck on screen), and the reference server sends 0.
            await self.send_engine_command(
                0x10, [self._cp(player), delta & 0xFFFFFFFF, 0, 0]
            )

            try:
                found = DUEL_MANAGER.find_duel_for_player(self.player_id)
                if found:
                    _, duel_data = found
                    native = duel_data.get("native")
                    if native and hasattr(native, "handle"):

                        pass
            except Exception:
                pass

        elif t == MSG["LPUPDATE"]:
            player = int(msg.get("player", 0))
            lp = int(msg.get("lp", 0))

            await self.send_engine_command(0x0F, [self._cp(player), lp, 0, 0])

        elif t == MSG["SHUFFLE_HAND"]:
            team = int(msg.get("player", 0))
            player = self._cp(team)
            available = list(self.m_hand[team])
            reordered = []
            for code in msg.get("cards", []):
                match = next(
                    (
                        i
                        for i, (u, _r) in enumerate(available)
                        if self.uid_to_code.get(u) == int(code)
                    ),
                    None,
                )
                if match is None:
                    continue
                reordered.append(available.pop(match))
            self.m_hand[team] = reordered + available
            await self._hand_refresh(team, 0)
            await self.send_engine_command(0x12, [player, 0, 0, 0])
        elif t == MSG["SHUFFLE_DECK"]:
            await self.send_engine_command(
                0x15, [self._cp(msg.get("player", 0)), 0, 0, 0]
            )
        elif t == MSG["ATTACK"]:
            card, target = msg.get("card", {}), msg.get("target")
            player = self._cp(card.get("controller", 0))
            zone = self._zone(card.get("location", LOC_MZONE), card.get("sequence", 0))
            if target:
                t_player = self._cp(target.get("controller", 0))
                t_zone = self._zone(
                    target.get("location", LOC_MZONE), target.get("sequence", 0)
                )
            else:
                t_player, t_zone = 1 - player, 16
            await self.send_engine_command(0xA, [0, 0, 0, 0])
            await self.send_engine_command(
                0xC, [player | (zone << 8), t_player, t_zone, 0]
            )
        elif t in (MSG["DAMAGE_STEP_END"], MSG["ATTACK_DISABLED"]):
            await self.send_engine_command(0xE, [0, 0, 0, 0])
        elif t == MSG["CHAINING"]:
            loc = int(msg.get("location", 0))
            zone = (
                self._zone(loc, msg.get("sequence", 0))
                if loc
                in (
                    LOC_MZONE,
                    LOC_SZONE,
                    LOC_HAND,
                    LOC_GRAVE,
                    LOC_REMOVED,
                    LOC_DECK,
                    LOC_EXTRA,
                )
                else 16
            )
            await self.send_engine_command(
                0x33,
                [
                    self._cp(msg.get("controller", 0)),
                    zone,
                    int(msg.get("chain_size", 1)),
                    0,
                ],
            )
        elif t == MSG["CHAIN_SOLVING"]:
            await self.send_engine_command(
                0x34, [0, 0, int(msg.get("chain_size", 1)), 0]
            )
        elif t == MSG["WIN"]:
            winner = int(msg.get("player", 0))
            if winner not in (0, 1):
                result = 3
            else:
                result = 1 if winner == self._client_team else 2
            await self._finish_duel(
                result, int(msg.get("reason", 0)) if result != 3 else 0
            )

    async def _finish_duel(self, result, reason):
        """Command 2 on the duel connection and 0x2009 on the front connection."""
        if self.duel_ended:
            return
        self.duel_ended = True
        await self.send_engine_command(2, [result, reason, 0, 0])
        record = STATE.player(self.player_id)
        if record is not None:
            record["n_duelall"] = int(record.get("n_duelall", 0)) + 1
            key = {1: "n_win", 2: "n_lose", 3: "n_draw"}[result]
            record[key] = int(record.get(key, 0)) + 1
            try:
                STATE.save()
            except Exception:
                log.exception("[Duel] Could not save the duel result")
        wins = int((record or {}).get("n_win", 0))
        losses = int((record or {}).get("n_lose", 0))
        draws = int((record or {}).get("n_draw", 0))
        result_data = MessageRecord(
            fields={
                "name": "resultData",
                "win": wins,
                "lose": losses,
                "draw": draws,
                "win_add": 1 if result == 1 else 0,
                "lose_add": 1 if result == 2 else 0,
                "draw_add": 1 if result == 3 else 0,
            }
        )
        duel_data = MessageRecord(
            tag=7,
            fields={
                "name": "duelData",
                "host": FRONT_ADDRESS,
                "port": DUEL_PORT,
                "time_total": DUEL_TOTAL_TIME,
                "time_turn": DUEL_TURN_TIME,
            },
            items=[
                MessageRecord(
                    fields={
                        "name": "gameServer",
                        "host": FRONT_ADDRESS,
                        "port": DUEL_PORT,
                    }
                ),
                MessageRecord(
                    fields={"name": "regulation", "regulation_id": REGULATION_ID}
                ),
                MessageRecord(
                    fields={
                        "name": "rule",
                        "match_type": 4,
                        "time_total": DUEL_TOTAL_TIME,
                        "time_turn": DUEL_TURN_TIME,
                    }
                ),
                MessageRecord(
                    fields={"name": "players"},
                    items=[
                        MessageRecord(
                            fields={"player_id": self.player_id}, items=[result_data]
                        )
                    ],
                ),
                MessageRecord(
                    fields={"name": "result", "duel_end": 1, "duel_result": result}
                ),
            ],
        )
        STATE.online_push(self.player_id, 0x2009, {}, [duel_data])
        self._report_to_front(result)
        front = STATE.online.get(self.player_id)
        if front is not None:
            front.npc_native_duel_id = None
        log.info("[Duel] Duel over: result=%s reason=%s", result, reason)

    def _report_to_front(self, result):
        """A duel between two players asks both for a manner rating (0x2CEC) and
        reports its winner to the user event it belongs to, once per duel.
        Duels against an NPC do neither."""
        found = DUEL_MANAGER.find_duel_for_player(self.player_id) if DUEL_MANAGER else None
        if found is None:
            return
        _, duel = found
        if duel.get("npc_player_id") is not None or duel.get("reported"):
            return
        duel["reported"] = True
        first, second = duel["players"]
        opponent = second if self.player_id == first else first
        request_evaluations(first, second)
        winner = {1: self.player_id, 2: opponent}.get(result)
        uev_report_result(self.player_id, opponent, winner)

    def _cmd_place(self, card):
        """The command map's key for an OCG card: hand index, monster slot as the
        index of zone 0, or a Spell/Trap or pile zone at index 0."""
        player = self._cp(card.get("controller", 0))
        loc, seq = int(card.get("location", 0)), int(card.get("sequence", 0))
        if loc == LOC_HAND:
            return self._table_place(player, 0x0B, seq)
        if loc == LOC_MZONE:
            return self._table_place(player, 0, seq)
        return self._table_place(player, self._zone(loc, seq), 0)

    # Sends the menu of what the card actions the player may take.
    async def _send_command_map(self, prompt):
        commands = {}

        def add(place, bit):
            commands[place] = commands.get(place, 0) | (1 << bit)

        flags = []
        if prompt["type"] == MSG["SELECT_IDLECMD"]:
            for card in prompt.get("summons", []):
                add(self._cmd_place(card), 4)
            for card in prompt.get("special_summons", []):
                add(self._cmd_place(card), 2)
            for card in prompt.get("monster_sets", []):
                add(self._cmd_place(card), 6)
            for card in prompt.get("spell_sets", []):
                add(self._cmd_place(card), 7)
            for card in prompt.get("activates", []):
                add(self._cmd_place(card), 3)
            for card in prompt.get("pos_changes", []):
                team, seq = int(card.get("controller", 0)), int(card.get("sequence", 0))
                entry = self.m_field.get(
                    (team, int(card.get("location", LOC_MZONE)), seq)
                )
                position = entry[2] if entry else POS_FACEUP_ATTACK

                bit = (
                    5
                    if position & POS_FACEDOWN_DEFENSE or position & POS_FACEDOWN_ATTACK
                    else 9 if position & POS_FACEUP_ATTACK else 8
                )
                add(self._cmd_place(card), bit)
            if prompt.get("to_bp"):
                flags.append((3, 1))
            if prompt.get("to_ep"):
                flags.append((5, 1))
            mode = 2
        else:
            for card in prompt.get("chains", []):
                add(self._cmd_place(card), 3)
            for card in prompt.get("attacks", []):
                add(self._cmd_place(card), 0)
            if prompt.get("to_m2"):
                flags.append((4, 1))
            if prompt.get("to_ep"):
                flags.append((5, 1))
            mode = 3

        client = self._client_team
        for team in (0, 1):
            player = self._cp(team)
            if self.m_pile.get((team, LOC_GRAVE)):
                add(self._table_place(player, 0x0E), 11)
            if self.m_pile.get((team, LOC_REMOVED)):
                add(self._table_place(player, 0x0F), 11)
        if any(self.extra_pool.get(client, {}).values()):
            add(self._table_place(0, 0x0C), 11)

        raw = bytearray()
        for place, mask in sorted(commands.items()):
            raw.extend(struct.pack(">II", place, mask))
        for key, value in flags:
            raw.extend(struct.pack("BB", key, value))
        await self.send_message(
            YO3_SERVER_DUEL,
            {
                "duelRequestName": 0x12,
                "comIDsize": len(commands),
                "helpIDsize": 0,
                "otherFlagSize": len(flags),
                "COMIDDATA": bytes(raw),
            },
        )
        await self.send_engine_command(4, [mode, 0, 0, 0])

    # A card as a row of the client's selection list.
    def _card_list_row(self, team, card):
        code = int(card.get("code", 0))
        loc = int(card.get("location", 0))
        found = self._lookup(team, loc, int(card.get("sequence", 0)), code)
        if found is None:
            return 0, 0, 0x8000 if self._cp(team) != 0 else 0
        uid, row, position = found
        zone = self._zone(loc, card.get("sequence", 0))
        row = self._visible_row(team, zone, position, row)
        return uid, row, (0x8000 if self._cp(team) != 0 else 0)

    async def _send_card_list(
        self,
        prompt,
        cards,
        *,
        minimum=1,
        maximum=1,
        can_cancel=False,
        title=0,
        list_player=None,
    ):
        """The client's card list (0x3EF/0x3F2/0x3F3 tables, then dialog 0x37)."""
        cards = list(cards or [])
        minimum = max(0, int(minimum))
        maximum = max(minimum, int(maximum))
        maximum = min(maximum, len(cards))
        if not cards:
            log.warning("[Duel] Refusing to open an empty card list")
            return False

        rows, marks, hidden = [], [], False
        default_team = int(prompt.get("player", 0))
        for card in cards:
            uid, row, mark = self._card_list_row(
                int(card.get("controller", default_team)), card
            )
            if row == 0:
                hidden = True
            rows.append(int(row))
            marks.append(mark)

        if hidden and minimum <= 1:
            kind = 0x4A
        elif can_cancel or minimum == 0:
            kind = 1
        elif minimum == 1:
            kind = 0x27
        else:
            maximum = minimum
            kind = 6

        self.pending_list_indices = []
        self.pending_list_prompt = prompt
        await self.send_engine_command(1007, [len(cards), maximum, 0, 0])
        for start in range(0, len(rows), 4):
            batch = rows[start : start + 4]
            await self.send_engine_command(1010, batch + [0] * (4 - len(batch)))
            batch_marks = marks[start : start + 4]
            await self.send_engine_command(
                1011, batch_marks + [0] * (4 - len(batch_marks))
            )
        await self.send_engine_command(
            0x37, [0 if list_player is None else int(list_player), kind, int(title)]
        )
        return True

    # Shows a selection dialog (cards, options, places, positions...).
    async def _send_option_dialog(self, prompt):
        options = list(prompt.get("options", []))[:4]
        if not options:
            return False
        await self.send_engine_command(1007, [len(options), 0, 0, 0])
        batch = [int(x) & 0x7FFFFFFF for x in options]
        await self.send_engine_command(1008, batch + [0] * (4 - len(batch)))
        await self.send_engine_command(0x36, [4, 276, 0])
        return True

    def _auto_places(self, prompt):
        """SELECT_PLACE / SELECT_DISFIELD: the client has no window for them, so
        take the first free zones. The engine's field mask has a bit set for a
        zone that is NOT available."""
        mask = int(prompt.get("field_mask", 0)) & 0xFFFFFFFF
        player, count = int(prompt.get("player", 0)), int(prompt.get("count", 1))
        if count == 0:
            return resp_places([(0, 0, 0)])
        legal = []
        for rel in (0, 1):
            absolute = player if rel == 0 else 1 - player
            base = 16 * rel
            for slot in range(5):
                if not mask & (1 << (base + slot)):
                    legal.append((absolute, LOC_MZONE, slot))
            for slot in range(5):
                if not mask & (1 << (base + 8 + slot)):
                    legal.append((absolute, LOC_SZONE, slot))
        log.info(
            "[Duel] %s: player=%s mask=%08x count=%s free=%s",
            MSG_NAME.get(prompt["type"]),
            player,
            mask,
            count,
            legal,
        )
        if len(legal) < count:
            log.error(
                "[Duel] Not enough free zones for %s", MSG_NAME.get(prompt["type"])
            )
            return None
        return resp_places(legal[:count])

    def _default_answer(self, prompt):
        """An answer for a prompt nobody is asked about (the CPU's, or one the
        client has no window for)."""
        t = prompt["type"]
        if t in (MSG["SELECT_PLACE"], MSG["SELECT_DISFIELD"]):
            return self._auto_places(prompt)
        if t in (MSG["SELECT_EFFECTYN"], MSG["SELECT_YESNO"]):
            return resp_bool(False)
        if t == MSG["SELECT_OPTION"]:
            return resp_index(0)
        if t == MSG["SELECT_CHAIN"]:
            return resp_chain(
                0 if prompt.get("forced") and prompt.get("selects") else None
            )
        if t in (MSG["SELECT_CARD"], MSG["SELECT_TRIBUTE"]):
            minimum = int(prompt.get("min", 1))
            if minimum == 0:
                return resp_cards(None) if prompt.get("can_cancel") else resp_cards([])
            return resp_cards(list(range(minimum)))
        if t == MSG["SELECT_POSITION"]:
            positions = int(prompt.get("positions", 0))
            for p in (
                POS_FACEUP_ATTACK,
                POS_FACEUP_DEFENSE,
                POS_FACEDOWN_DEFENSE,
                POS_FACEDOWN_ATTACK,
            ):
                if positions & p:
                    return resp_position(p)
            return resp_position(POS_FACEUP_ATTACK)
        if t in (MSG["SORT_CARD"], MSG["SORT_CHAIN"]):
            return resp_sort(None)
        if t == MSG["SELECT_COUNTER"]:
            remaining, counts = int(prompt.get("count", 0)), []
            for card in prompt.get("cards", []):
                take = min(remaining, int(card.get("count", 0)))
                counts.append(take)
                remaining -= take
            return resp_counters(counts)
        if t == MSG["SELECT_UNSELECT_CARD"]:
            if prompt.get("can_finish"):
                return resp_unselect(None)
            return (
                resp_unselect(0) if prompt.get("select_cards") else resp_unselect(None)
            )
        if t == MSG["ANNOUNCE_RACE"]:
            available = int(prompt.get("available", 1))
            return resp_race(available & -available if available else 1)
        if t == MSG["ANNOUNCE_ATTRIB"]:
            available = int(prompt.get("available", 1))
            return resp_attrib(available & -available if available else 1)
        if t == MSG["ANNOUNCE_NUMBER"]:
            return resp_number(0)
        log.warning("[Duel] No default answer for %s", MSG_NAME.get(t, t))
        return None

    def _cpu_answer(self, prompt):
        """The CPU's play: Normal Summon or Set what it can, Set Spells/Traps, then
        battle using any legal attack (direct or against a monster), and end the turn."""
        t = prompt["type"]
        if t == MSG["SELECT_IDLECMD"]:
            if prompt.get("summons"):
                return resp_idle(0, 0)
            if prompt.get("monster_sets"):
                return resp_idle(3, 0)
            if prompt.get("spell_sets"):
                return resp_idle(4, 0)
            if prompt.get("to_bp"):
                return resp_idle(6)
            return resp_idle(7)
        if t == MSG["SELECT_BATTLECMD"]:
            attacks = prompt.get("attacks", [])
            # The engine includes attacks against opposing monsters as well as
            # direct attacks. Prefer a direct attack when legal, but do not pass
            # Battle Phase just because the opponent controls a monster.
            attack_index = next(
                (i for i, attack in enumerate(attacks) if attack.get("can_direct")),
                0 if attacks else None,
            )
            if attack_index is not None:
                return resp_battle(1, attack_index)
            return resp_battle(3) if prompt.get("to_ep") else resp_battle(2)
        return self._default_answer(prompt)

    async def _prompt_for_client(self, duel, prompt, events):
        """Show a prompt on the client. Returns an answer when the client has no
        window for it, else None (the client will answer)."""
        t = prompt["type"]
        self.pending_response_kind = None
        if t in (MSG["SELECT_IDLECMD"], MSG["SELECT_BATTLECMD"]):
            await self._send_command_map(prompt)
        elif t == MSG["SELECT_EFFECTYN"]:
            self.pending_response_kind = "bool"
            await self.send_engine_command(0x36, [3, 250, 4])
        elif t == MSG["SELECT_YESNO"]:
            self.pending_response_kind = "bool"
            await self.send_engine_command(0x36, [2, 270, 0])
        elif t == MSG["SELECT_POSITION"]:
            team = int(prompt.get("player", 0))
            uid = 0
            for pool in (self.uid_by_team_code, self.extra_pool):
                entries = pool.get(team, {}).get(int(prompt.get("code", 0)))
                if entries:
                    uid = entries[0][0]
                    break
            if not uid:
                for entry in list(self.m_hand[team]) + [
                    (v[0], v[1]) for v in self.m_field.values()
                ]:
                    if self.uid_to_code.get(entry[0]) == int(prompt.get("code", 0)):
                        uid = entry[0]
                        break
            self.pending_response_kind = "position"
            positions = int(prompt.get("positions", 0))
            flag = (1 << 16) if positions & POS_FACEUP_DEFENSE else 0
            await self.send_engine_command(0x36, [8, 327, (uid & 0xFFFF) | flag])
        elif t == MSG["SELECT_OPTION"]:
            if not await self._send_option_dialog(prompt):
                return self._default_answer(prompt)
        elif t in (MSG["SELECT_CARD"], MSG["SELECT_TRIBUTE"]):
            hint_title = {
                500: 18,
                501: 347,
                502: 281,
                503: 70,
                504: 58,
                505: 348,
                506: 88,
                507: 78,
                508: 39,
                509: 25,
                510: 46,
                514: 94,
                518: 101,
                520: 119,
                526: 341,
                528: 357,
                549: 222,
                551: 94,
            }
            hint = next(
                (
                    int(e.get("hint", 0))
                    for e in reversed(events)
                    if e.get("type") == MSG["HINT"] and e.get("hint_type") == 3
                ),
                0,
            )
            if not await self._send_card_list(
                prompt,
                prompt.get("selects", []),
                minimum=int(prompt.get("min", 1)),
                maximum=int(prompt.get("max", 1)),
                can_cancel=bool(prompt.get("can_cancel")),
                title=hint_title.get(hint, 0),
            ):
                return self._default_answer(prompt)
        elif t == MSG["SELECT_CHAIN"]:
            cards = prompt.get("selects", [])
            if not cards:
                return resp_chain(None)
            if not await self._send_card_list(
                prompt,
                cards,
                minimum=1,
                maximum=1,
                can_cancel=not bool(prompt.get("forced")),
                title=276,
            ):
                return resp_chain(None)
        elif t == MSG["SELECT_UNSELECT_CARD"]:
            cards = prompt.get("select_cards", [])
            if not await self._send_card_list(
                prompt,
                cards,
                minimum=1,
                maximum=1,
                can_cancel=bool(prompt.get("can_cancel") or prompt.get("can_finish")),
                title=92,
            ):
                return self._default_answer(prompt)
        else:
            return self._default_answer(prompt)
        return None

    # Translates a batch of events, then the prompt, if any.
    async def _translate(self, duel, events):
        for msg in events:
            if msg.get("type") == MSG["RETRY"]:
                log.warning("[Duel] The engine refused the last answer (MSG_RETRY)")
            await self._translate_event(msg)
        prompt = next((m for m in reversed(events) if m.get("type") in PROMPTS), None)
        duel["pending_prompt"] = prompt
        if (
            prompt is not None
            and int(prompt.get("player", 0)) == self._client_team
            and not self.duel_ended
        ):
            return await self._prompt_for_client(duel, prompt, events)
        return None

    async def _pump(self, duel_id, duel, events, status):
        """Translate engine output for the client; answer the CPU's prompts (and
        those the client has no window for) until the client has to act."""
        for _ in range(600):
            auto = await self._translate(duel, events)
            if status == OCG_DUEL_STATUS_END:
                duel["ended"] = True
                if not self.duel_ended:
                    await self._finish_duel(3, 0)
                return
            if self.duel_ended:
                return
            prompt = duel.get("pending_prompt")
            if prompt is None:
                return
            if int(prompt.get("player", 0)) == self._client_team:
                if auto is None:
                    return
                response = auto
            else:
                response = self._cpu_answer(prompt)
            if response is None:
                log.error(
                    "[Duel] Nobody can answer %s",
                    MSG_NAME.get(prompt["type"], prompt["type"]),
                )
                return
            status, raw = DUEL_MANAGER.respond_duel(duel_id, response)
            events = parse_messages(raw, strict=False) if raw else []
            duel["ocg_status"] = status
            if events:
                log.info(
                    "[Duel] Engine: %s",
                    [MSG_NAME.get(m["type"], m["type"]) for m in events],
                )
        log.error("[Duel] The prompt loop did not settle after 600 answers")

    # Answers the engine and pumps it until the next prompt.
    async def _advance_core(self, duel_id, duel, response):
        status, raw = DUEL_MANAGER.respond_duel(duel_id, response)
        events = parse_messages(raw, strict=False) if raw else []
        duel["ocg_status"] = status
        if events:
            log.info(
                "[Duel] Engine: %s",
                [MSG_NAME.get(m["type"], m["type"]) for m in events],
            )
        await self._pump(duel_id, duel, events, status)

    def _menu_place(self, zone, index):
        """The place a card menu was opened on -> (OCG location, sequence)."""
        if zone == 0x0B:
            return LOC_HAND, index
        if zone == 0:
            return LOC_MZONE, index
        if 1 <= zone <= 4:
            return LOC_MZONE, zone
        if 5 <= zone <= 9:
            return LOC_SZONE, zone - 5
        if zone == 10:
            return LOC_SZONE, 5
        return {
            0x0C: LOC_EXTRA,
            0x0D: LOC_DECK,
            0x0E: LOC_GRAVE,
            0x0F: LOC_REMOVED,
        }.get(zone, 0), index

    # True if a mirrored card sits at the given place.
    @staticmethod
    def _matches(card, team, location, sequence):
        if (
            int(card.get("controller", -1)) != team
            or int(card.get("location", 0)) != location
        ):
            return False
        return (
            location in (LOC_EXTRA, LOC_GRAVE, LOC_REMOVED, LOC_DECK)
            or int(card.get("sequence", -1)) == sequence
        )

    # A card menu command from the client (summon, set, activate, attack...).
    async def _handle_menu(self, duel_id, duel, fields):
        prompt = duel.get("pending_prompt") or {}
        ptype = prompt.get("type")
        command = int(fields.get("CommandID", fields.get("command_id", -1)))
        team = self._team(int(fields.get("player", 0)))
        zone, index = int(fields.get("position", 0)), int(fields.get("index", 0))

        if ptype not in (MSG["SELECT_IDLECMD"], MSG["SELECT_BATTLECMD"]):
            log.warning(
                "[Duel] Card command %s with no idle/battle prompt pending (%s)",
                command,
                ptype,
            )
            return
        if command in (1, 10, 11, 12, 13):
            return

        if ptype == MSG["SELECT_IDLECMD"]:
            table = {
                4: ("summons", 0),
                2: ("special_summons", 1),
                6: ("monster_sets", 3),
                7: ("spell_sets", 4),
                3: ("activates", 5),
                5: ("pos_changes", 2),
                8: ("pos_changes", 2),
                9: ("pos_changes", 2),
            }
        else:
            table = {0: ("attacks", 1), 3: ("chains", 0)}
        entry = table.get(command)
        if entry is None:
            log.warning("[Duel] Unsupported card command %s", command)
            return
        key, action = entry
        location, sequence = self._menu_place(zone, index)
        listed = prompt.get(key, [])
        candidates = [
            i
            for i, c in enumerate(listed)
            if self._matches(c, team, location, sequence)
        ]

        if (
            not candidates
            and location == LOC_HAND
            and 0 <= index < len(self.m_hand[team])
        ):

            code = self.uid_to_code.get(self.m_hand[team][index][0])
            candidates = [
                i
                for i, c in enumerate(listed)
                if int(c.get("controller", -1)) == team
                and int(c.get("location", 0)) == LOC_HAND
                and int(c.get("code", 0)) == code
            ]

        if not candidates:
            log.warning(
                "[Duel] Command %s at zone=%s index=%s matches nothing in %s",
                command,
                zone,
                index,
                key,
            )
            await self._send_command_map(prompt)
            return

        pile = location in (LOC_EXTRA, LOC_GRAVE, LOC_REMOVED, LOC_DECK)
        if pile and len({int(listed[i].get("code", 0)) for i in candidates}) > 1:
            duel["list_action"] = (key, action, candidates)
            title = 26 if location == LOC_EXTRA else (276 if command == 3 else 0)
            await self._send_card_list(
                prompt,
                [listed[i] for i in candidates],
                minimum=1,
                maximum=1,
                can_cancel=True,
                title=title,
            )
            return

        await self._advance_core(duel_id, duel, resp_idle(action, candidates[0]))

    # A phase-change command from the client.
    async def _handle_phase(self, duel_id, duel, fields):
        phase = int(fields.get("phase", -1))
        prompt = duel.get("pending_prompt") or {}
        ptype = prompt.get("type")
        response = None
        if ptype == MSG["SELECT_IDLECMD"]:
            if phase == 3 and prompt.get("to_bp"):
                response = resp_idle(6)
            elif phase == 5 and prompt.get("to_ep"):
                response = resp_idle(7)
        elif ptype == MSG["SELECT_BATTLECMD"]:
            if phase == 4 and prompt.get("to_m2"):
                response = resp_battle(2)
            elif phase == 5 and prompt.get("to_ep"):
                response = resp_battle(3)
        if response is None:
            log.warning(
                "[Duel] Phase button %r is not available now (%s)",
                phase,
                MSG_NAME.get(ptype, ptype),
            )
            return
        await self._advance_core(duel_id, duel, response)

    # The client's answer to a dialog.
    async def _handle_dialog(self, duel_id, duel, fields):
        prompt = duel.get("pending_prompt") or {}
        ptype = prompt.get("type")
        choice = int(fields.get("DLGResult", 0))
        picks = self.pending_list_indices[: max(0, choice)]
        self.pending_list_indices = []

        action = duel.pop("list_action", None)
        if action is not None:
            key, code, candidates = action
            if not picks:
                await self._send_command_map(prompt)
                return
            await self._advance_core(
                duel_id, duel, resp_idle(code, candidates[picks[0]])
            )
            return

        if ptype in (MSG["SELECT_EFFECTYN"], MSG["SELECT_YESNO"]):
            response = resp_bool(choice != 0)
        elif ptype == MSG["SELECT_POSITION"]:
            positions = int(prompt.get("positions", 0))
            legal = [
                p
                for p in (
                    POS_FACEUP_ATTACK,
                    POS_FACEDOWN_ATTACK,
                    POS_FACEUP_DEFENSE,
                    POS_FACEDOWN_DEFENSE,
                )
                if positions & p
            ]
            if not legal:
                raise ValueError("SELECT_POSITION has no legal position")
            if choice == 1:
                response = resp_position(
                    POS_FACEUP_DEFENSE
                    if POS_FACEUP_DEFENSE in legal
                    else (
                        POS_FACEDOWN_DEFENSE
                        if POS_FACEDOWN_DEFENSE in legal
                        else legal[0]
                    )
                )
            else:
                response = resp_position(
                    POS_FACEUP_ATTACK
                    if POS_FACEUP_ATTACK in legal
                    else (
                        POS_FACEDOWN_ATTACK
                        if POS_FACEDOWN_ATTACK in legal
                        else legal[0]
                    )
                )
        elif ptype == MSG["SELECT_OPTION"]:
            response = resp_index(choice)
        elif ptype == MSG["SELECT_CHAIN"]:
            response = resp_chain(picks[0] if picks else None)
        elif ptype == MSG["SELECT_UNSELECT_CARD"]:
            response = resp_unselect(picks[0] if picks else None)
        elif ptype in (MSG["SELECT_CARD"], MSG["SELECT_TRIBUTE"]):
            if not picks and prompt.get("can_cancel"):
                response = resp_cards(None)
            else:
                response = resp_cards(picks)
        else:
            log.warning(
                "[Duel] Dialog answer with no matching prompt: %s",
                MSG_NAME.get(ptype, ptype),
            )
            return
        await self._advance_core(duel_id, duel, response)

    # Routes a client duel command by its name id.
    async def handle_client_command(self, player_id, message, packet):
        fields = message.get("fields", {})
        request_id = fields.get("request")
        command_name = fields.get("duelRequestName")
        found = DUEL_MANAGER.find_duel_for_player(player_id)
        if found is None or request_id != YO3_CLIENT_DUEL:
            return

        duel_id, duel = found

        if command_name == YO3_SELECT_HAND:
            if not self.Roshambo_complete and isinstance(fields.get("selectHand"), int):
                await self.resolve_Roshambo(fields["selectHand"])
            return

        if command_name == YO3_SELECT_FIRST:
            if self.Roshambo_complete and not self.turn_order_complete:
                choice = fields.get("player", fields.get("choice", 0))
                await self.resolve_turn_order(YO3_FIRST if choice == 1 else YO3_SECOND)
            return

        if command_name == 0x0F:
            duel["auto_set"] = bool(fields.get("autoSet", 0))
            return

        if self.duel_ended or not self.turn_order_complete:
            return

        try:
            if command_name == 2:
                await self._handle_menu(duel_id, duel, fields)
            elif command_name == 3:
                await self._handle_phase(duel_id, duel, fields)
            elif command_name == 4:
                await self._handle_dialog(duel_id, duel, fields)
            elif command_name == 5:

                prompt = duel.get("pending_prompt")
                if prompt and prompt.get("type") in (
                    MSG["SELECT_IDLECMD"],
                    MSG["SELECT_BATTLECMD"],
                ):
                    await self._send_command_map(prompt)
            elif command_name in (6, 7):
                if command_name == 6:
                    self.pending_list_indices = []
                    index = int(fields.get("SetIndex", fields.get("index", -1)))
                else:
                    index = int(fields.get("index", -1))
                if index >= 0 and index not in self.pending_list_indices:
                    self.pending_list_indices.append(index)
            elif command_name == 0x11:
                response = fields.get("response", b"")
                if isinstance(response, int):
                    response = struct.pack("<I", response)
                if not isinstance(response, bytes):
                    raise ValueError("OCG response must be bytes")
                await self._advance_core(duel_id, duel, response)
        except Exception:
            log.exception(
                "[Duel] Failed to process client action: player=%s command=%r fields=%r",
                player_id,
                command_name,
                fields,
            )

    # Duel connection loop: entry (0x200A), opening setup, then commands until the duel ends.
    async def handle(self):
        global DUEL_MANAGER
        log.info("[Duel] Connection from %s", self.addr)
        try:
            if DUEL_MANAGER is None:
                raise RuntimeError("Duel manager is unavailable")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 15
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                packet = await asyncio.wait_for(
                    read_frame(self.reader), timeout=remaining
                )
                message, consumed = decode_message(packet)
                fields = message["fields"]
                request_id = fields.get("request")
                log.info(
                    "[Duel] Received packet request=%r fields=%r items=%d",
                    request_id,
                    fields,
                    len(message.get("items", [])),
                )
                if request_id != 0x2724:
                    break

            if request_id != YO3_ENTER:
                raise ValueError(f"Expected 0x200A, received {request_id!r}")

            player_id = fields.get("player_id")
            if not isinstance(player_id, int) or player_id <= 0:
                raise ValueError("Invalid player_id in 0x200A")

            found = DUEL_MANAGER.find_duel_for_player(player_id)
            if found is None:
                raise ValueError(f"Player {player_id} has no active assigned duel")

            duel_id, duel = found
            players = tuple(duel["players"])
            connection_key = (duel_id, player_id)

            if connection_key in DUEL_CONNECTIONS:
                raise ValueError(
                    f"Player {player_id} already connected to duel {duel_id}"
                )

            self.player_id, self.duel_id = player_id, duel_id
            DUEL_CONNECTIONS[connection_key] = self
            opponent_id = next((p for p in players if p != player_id), 0)
            for event in list(STATE.user_events.values()):  # an event's table is taken
                for event_duel_id, event_duel in (event.get("run") or {}).get("duels", {}).items():
                    if {player_id, opponent_id} == set(event_duel["players"]):
                        uev_duel_began(event_duel_id)

            await self.process_initial_core_state(duel_id, duel)
            await self.send_engine_command(
                14, [self.turn_timeout, self.turn_timeout, 0, 0]
            )
            await self.begin_Roshambo()

            while not self.closed:
                packet = await asyncio.wait_for(read_frame(self.reader), timeout=180)
                message, consumed = decode_message(packet)
                fields = message["fields"]
                request_id = fields.get("request")
                log.info(
                    "[Duel] Gameplay packet request=%r fields=%r items=%d",
                    request_id,
                    fields,
                    len(message.get("items", [])),
                )
                if request_id == 0x2724:
                    self.heartbeats += 1
                    continue
                if request_id == YO3_CLIENT_DUEL:
                    await self.handle_client_command(player_id, message, packet)
                    continue
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            log.info("[Duel] Disconnected or timed out: %s", self.addr)
        except Exception:
            log.exception("[Duel] Session error: %s", self.addr)
        finally:
            self.closed = True
            if self.duel_id is not None and self.player_id is not None:
                DUEL_CONNECTIONS.pop((self.duel_id, self.player_id), None)
                if self.duel_ended and DUEL_MANAGER is not None:
                    try:
                        DUEL_MANAGER.remove_duel(self.duel_id)
                    except Exception:
                        log.exception("[Duel] Could not remove the finished duel")
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass


# Entry point for a connection on the duel port.
async def duel_client(reader, writer):
    session = DuelSession(reader, writer)
    await session.handle()


# Entry point for a connection on the gate port.
async def gate_client(reader, writer):
    session = ClientSession(reader, writer, "Gate")
    await session.handle()


# Entry point for a connection on the front port.
async def front_client(reader, writer):
    session = ClientSession(reader, writer, "Front")
    await session.handle()


# Writes a minimal HTTP/1.1 response.
async def send_http(
    writer, status, body, content_type="text/plain", content_length=None
):
    if content_length is None:
        content_length = len(body)
    header = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {content_length}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(header)
    if body:
        writer.write(body)
    await writer.drain()


# Serves GET/HEAD for files under /yo3/, /ygoda/patch/ and /app/ from the www folder
# (path traversal is refused).
async def http_client(reader, writer):
    addr = writer.get_extra_info("peername")
    try:
        data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        request_line = data.decode("iso-8859-1").split("\r\n", 1)[0]
        parts = request_line.split()
        if len(parts) != 3:
            await send_http(writer, "400 Bad Request", b"Bad request")
            return

        method, target, _ = parts
        if method not in ("GET", "HEAD"):
            await send_http(writer, "405 Method Not Allowed", b"Method not allowed")
            return

        path = unquote(urlsplit(target).path)
        if not path.startswith(("/yo3/", "/ygoda/patch/", "/app/")):
            await send_http(writer, "404 Not Found", b"Not found")
            return

        file_path = (WEB_ROOT / path.lstrip("/")).resolve()
        try:
            file_path.relative_to(WEB_ROOT.resolve())
        except ValueError:
            await send_http(writer, "403 Forbidden", b"Forbidden")
            return

        if not file_path.is_file():
            await send_http(writer, "404 Not Found", b"File not found")
            return

        body = await asyncio.to_thread(file_path.read_bytes)
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        if method == "HEAD":
            await send_http(
                writer, "200 OK", b"", content_type, content_length=len(body)
            )
        else:
            await send_http(writer, "200 OK", body, content_type)
    except Exception:
        log.exception("[HTTP] Error handling %s", addr)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# Starts the gate, front, duel and HTTP servers plus the rental ticker, and runs until stopped.
async def main():
    global DUEL_ENGINE, DUEL_MANAGER, FRONT_MATCHMAKER

    init_game_data()
    servers = []
    ticker = None

    try:
        if GAME.loaded:
            try:
                DUEL_ENGINE = DuelEngine()
                DUEL_MANAGER = DuelManager(DUEL_ENGINE, GAME.cards)

                FRONT_MATCHMAKER = FrontMatchmakingRouter(
                    DUEL_MANAGER,
                    default_duel_host=FRONT_ADDRESS,
                    default_duel_port=DUEL_PORT,
                )

            except Exception:
                log.exception("Duel engine initialisation failed")

                if DUEL_MANAGER:
                    try:
                        DUEL_MANAGER.close()
                    except Exception:
                        log.exception("Failed to close duel manager")

                if DUEL_ENGINE:
                    try:
                        DUEL_ENGINE.close()
                    except Exception:
                        log.exception("Failed to close duel engine")

                DUEL_ENGINE = None
                DUEL_MANAGER = None
                FRONT_MATCHMAKER = None

        if not STATE.chat_rooms:
            STATE.chat_rooms[1] = {
                "name": "Main Room",
                "members": set(),
            }

        for handler, port in (
            (gate_client, GATE_PORT),
            (front_client, FRONT_PORT),
            (duel_client, DUEL_PORT),
            (http_client, HTTP_PORT),
        ):
            server = await asyncio.start_server(
                handler,
                HOST,
                port,
            )
            servers.append(server)

        gate, front, duel, http = servers

        log.info("======================================")
        log.info("          YGO3 PRIVATE SERVER")
        log.info("======================================")
        log.info("Gate:  %s:%d", HOST, GATE_PORT)
        log.info("Front: %s:%d", HOST, FRONT_PORT)
        log.info("Duel:  %s:%d", HOST, DUEL_PORT)
        log.info("HTTP:  %s:%d", HOST, HTTP_PORT)
        log.info("Game data: %s", describe_game_data())
        log.info("======================================")

        ticker = asyncio.create_task(rental_ticker())  # ends rentals at their term
        async with gate, front, duel, http:
            await asyncio.gather(
                gate.serve_forever(),
                front.serve_forever(),
                duel.serve_forever(),
                http.serve_forever(),
            )

    finally:
        if ticker is not None:
            ticker.cancel()
        for server in servers:
            server.close()

        if servers:
            await asyncio.gather(
                *(server.wait_closed() for server in servers),
                return_exceptions=True,
            )

        if DUEL_MANAGER:
            try:
                DUEL_MANAGER.close()
            except Exception:
                log.exception("Failed to close duel manager")

        if DUEL_ENGINE:
            try:
                DUEL_ENGINE.close()
            except Exception:
                log.exception("Failed to close duel engine")

        DUEL_MANAGER = None
        DUEL_ENGINE = None
        FRONT_MATCHMAKER = None

        try:
            STATE.save()
        except Exception:
            log.exception("Failed to save server state")


# Reads the values after a CLI flag, exiting with usage if too few.
def cli_values(flag, minimum, usage):
    try:
        index = sys.argv.index(flag)
    except ValueError:
        print(f"Usage: python game_server.py {usage}")
        raise SystemExit(1)

    values = sys.argv[index + 1 :]

    for position, value in enumerate(values):
        if value.startswith("--"):
            values = values[:position]
            break

    if len(values) < minimum:
        print(f"Usage: python game_server.py {usage}")
        raise SystemExit(1)

    return values


# Loads the game data or exits.
def require_game_data():
    init_game_data()

    if not GAME.loaded:
        print("Game data could not be loaded.")
        raise SystemExit(1)


if __name__ == "__main__":

    if "--check-game" in sys.argv:
        init_game_data()

        print(f"Game dir: {GAME_DIR}")
        print(f"Game data: {describe_game_data()}")

        raise SystemExit(0 if GAME.loaded else 1)

    elif "--card-search" in sys.argv:
        values = cli_values(
            "--card-search",
            1,
            '--card-search "Dark Magician"',
        )

        query = " ".join(values)

        require_game_data()

        for card in GAME.search(query):
            print(
                f"row {card.row}",
                f"cid {card.cid}",
                card.name,
                sep="\t",
            )

    elif "--grant-bp" in sys.argv:
        values = cli_values(
            "--grant-bp",
            1,
            "--grant-bp AMOUNT [USER_ID]",
        )
        try:
            amount = int(values[0])
            user_id = int(values[1]) if len(values) > 1 else USER_ID

            account = STATE.get_account(user_id)
            account.duel_points = max(0, int(account.duel_points) + amount)
            STATE.save()

            print(
                f"Successfully added {amount} BP to User {user_id}. New balance: {account.duel_points} BP"
            )
        except ValueError as exc:
            print(f"Could not grant BP: {exc}")
            raise SystemExit(1)

    elif "--grant-card" in sys.argv:
        values = cli_values(
            "--grant-card",
            2,
            "--grant-card PLAYER_ID ROW [QUANTITY]",
        )

        require_game_data()

        try:
            player_id = int(values[0])
            row = int(values[1])
            quantity = int(values[2]) if len(values) > 2 else 1

            if quantity < 1:
                raise ValueError("Quantity must be at least 1")

            if row not in GAME.cards:
                raise ValueError(f"Unknown card row: {row}")

            grant_card(player_id, row, quantity)

        except (ValueError, KeyError) as exc:
            print(f"Could not grant the card: {exc}")
            raise SystemExit(1)

        print(
            f"Granted {quantity} copies of row {row} "
            f"({GAME.cards[row].name}) to player {player_id}"
        )

    elif "--resolve-check" in sys.argv:
        require_game_data()

        database = CardDatabase(DEFAULT_DATABASE)
        failures = []

        try:
            for card in GAME.cards.values():
                try:
                    database.resolve_yo3_card(card)
                except DuelDeckError as exc:
                    failures.append((card, str(exc)))

        finally:
            database.close()

        resolved = len(GAME.cards) - len(failures)

        print(f"Resolved {resolved} of {len(GAME.cards)} cards")

        for card, error in failures:
            print(f"FAILED: row {card.row} " f"({card.name}): {error}")

        raise SystemExit(1 if failures else 0)

    elif "--engine-check" in sys.argv:
        require_game_data()

        engine = DuelEngine()
        manager = None

        try:
            manager = DuelManager(engine, GAME.cards)

            print(f"Loaded OCG core " f"{engine.major}.{engine.minor}")

            if GAME.starter_deck is None:
                raise SystemExit("No starter deck found to test with")

            deck = preset_payload(GAME.starter_deck)

            duel_id = manager.create_duel(
                1,
                2,
                deck,
                deck,
            )

            status, messages = manager.process_duel(duel_id)

            print(f"First prompt: status={status}, " f"{len(messages)} message bytes")

        finally:
            if manager:
                manager.close()

            engine.close()

    elif "--inventory" in sys.argv:
        values = cli_values(
            "--inventory",
            1,
            "--inventory PLAYER_ID",
        )

        require_game_data()

        try:
            player_id = int(values[0])
            inventory = get_inventory(player_id)

        except ValueError as exc:
            print(exc)
            raise SystemExit(1)

        for card, count in inventory:
            print(
                f"row {card.row}",
                card.name,
                f"x{count}",
                sep="\t",
            )

    else:
        try:
            asyncio.run(main())

        except KeyboardInterrupt:
            log.info("Server stopped.")