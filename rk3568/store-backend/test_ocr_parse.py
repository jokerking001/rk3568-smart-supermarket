#!/usr/bin/env python3
"""Unit tests for the OCR label parser and catalog matcher.

The OCR engine is not installed on the board, but the parsing and matching logic is
the part that turns raw text into a SKU decision, and it is fully testable offline.
These tests use realistic OCR strings, including the noisy output a curved bottle
actually produces.

Run:  python3 test_ocr_parse.py
"""

import sys

import ocr_service as ocr

PASSED = []
FAILED = []

CATALOG = [
    {"code": "6901234567890", "name": "可口可乐 330ml", "price": 3.50},
    {"code": "6901234567894", "name": "农夫山泉 550ml", "price": 2.00},
    {"code": "6901234567896", "name": "蒙牛纯牛奶 250ml", "price": 3.80},
    {"code": "6901234567895", "name": "乐事薯片 75g", "price": 7.50},
]


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s %s" % (name, detail))


def main():
    print("== ocr parse tests ==")

    # --- capacity extraction ---------------------------------------------
    cases = [
        ("可口可乐 净含量 330ml", "330", "ml"),
        ("农夫山泉 550毫升", "550", "ml"),
        ("蒙牛纯牛奶 净含量: 250 mL", "250", "ml"),
        ("乐事薯片 75g", "75", "g"),
        ("某饮料 1.5L", "1.5", "L"),
        ("咖啡豆 500克", "500", "g"),
    ]
    for text, value, unit in cases:
        got_value, got_unit, _ = ocr.extract_capacity(text)
        check("capacity %r -> %s%s" % (text, value, unit),
              got_value == value and got_unit == unit,
              "got %s%s" % (got_value, got_unit))

    check("no capacity yields None",
          ocr.extract_capacity("可口可乐 经典口味")[0] is None)

    # --- noise removal ----------------------------------------------------
    noisy = "可口可乐 330ml\n净含量：330毫升\n生产日期：见瓶身\n配料：水、糖\n保质期：12个月"
    cleaned = ocr.normalize(noisy)
    check("boilerplate lines are dropped", "配料" not in cleaned and "保质期" not in cleaned,
          repr(cleaned))
    check("product text survives cleaning", "可口可乐" in cleaned, repr(cleaned))

    # --- catalog matching -------------------------------------------------
    result = ocr.parse_label("可口可乐 净含量 330ml", CATALOG)
    check("brand resolves to the right SKU",
          result["candidates"] and result["candidates"][0]["code"] == "6901234567890",
          "%s" % result["candidates"][:2])
    check("exact name match scores 1.0",
          result["candidates"] and result["candidates"][0]["score"] == 1.0,
          "%s" % result["candidates"][:2])
    check("result is marked confident", result["confident"] is True, str(result))

    result = ocr.parse_label("农夫山泉 饮用天然水 550ml", CATALOG)
    check("partial-name match still finds the SKU",
          result["candidates"] and result["candidates"][0]["code"] == "6901234567894",
          "%s" % result["candidates"][:2])

    # --- OCR noise must not produce a confident wrong answer --------------
    result = ocr.parse_label("8 8 8 3 3 0 ￥ 4.0 0 半 价 促 销", CATALOG)
    check("garbage text produces no confident match", result["confident"] is False,
          "confident=%s candidates=%s" % (result["confident"], result["candidates"]))

    result = ocr.parse_label("", CATALOG)
    check("empty text is handled", result["candidates"] == [] and result["brand"] == "",
          str(result))

    # --- fallback when the catalog is unreachable ------------------------
    result = ocr.parse_label("可口可乐 净含量 330ml", None)
    check("works without a catalog", result["capacity_value"] == "330"
          and result["brand"].startswith("可口可乐"), str(result))

    # --- similarity helper ------------------------------------------------
    check("longest common substring is correct",
          ocr.longest_common_substring("abcdef", "zzcdezz") == 3,
          "%d" % ocr.longest_common_substring("abcdef", "zzcdezz"))
    check("longest common substring handles empty input",
          ocr.longest_common_substring("", "abc") == 0)

    # --- engine honesty ---------------------------------------------------
    info = ocr.engine_info()
    check("engine_info reports availability without raising",
          "available" in info and "install_hint" in info, str(info))

    print("\n== %d passed, %d failed ==" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
