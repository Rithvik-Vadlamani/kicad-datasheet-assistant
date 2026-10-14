"""Run with KiCad's Python interpreter, not the application venv (KiCad 10 only)."""

import json
import sys

import pcbnew


def main():
    request = json.load(sys.stdin)
    if pcbnew.GetBuildVersion().split(".")[0] != "10":
        raise ValueError("Native placement worker targets KiCad 10")
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(request["copper_layers"])
    positions = {}
    for entry in request["footprints"]:
        footprint = pcbnew.FootprintLoad(entry["library"], entry["name"])
        if footprint is None:
            raise ValueError("Cannot load native footprint " + entry["name"])
        board.Add(footprint)
        footprint.SetReference(entry["ref"])
        footprint.SetPosition(pcbnew.VECTOR2I(*(round(v * 1e6) for v in entry["at"])))
        if entry["side"] == "bottom":
            footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        # The requested angle is the final KiCad footprint orientation on its chosen side.
        footprint.SetOrientationDegrees(entry["angle"])
        positions[entry["ref"]] = {}
        for pad in footprint.Pads():
            p = pad.GetPosition()
            positions[entry["ref"]].setdefault(pad.GetNumber(), []).append([p.x / 1e6, p.y / 1e6])
    pcbnew.SaveBoard(request["output"], board)
    print(json.dumps({"positions": positions, "tool_version": pcbnew.GetBuildVersion()}))


if __name__ == "__main__":
    main()
