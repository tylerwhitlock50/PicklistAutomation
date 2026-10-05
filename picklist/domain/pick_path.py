"""Warehouse walking order: highest rack first, then highest shelf."""
import re


def pick_line_sort_key(line: dict) -> tuple:
    location = str(line.get("location") or "").strip().upper()
    rack = re.fullmatch(r"R(\d+)S(\d+)(.*)", location)
    # Non-rack locations follow the rack route in a stable natural order.
    natural = tuple((0, int(piece)) if piece.isdigit() else (1, piece)
                    for piece in re.split(r"(\d+)", location) if piece)
    prefix = (0, -int(rack[1]), -int(rack[2]), rack[3]) if rack else (1, 0, 0, "")
    return (*prefix, natural, str(line.get("cust_order_id") or ""),
            str(line.get("part_id") or ""), line.get("id", 0))
