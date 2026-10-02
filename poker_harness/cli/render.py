"""Turn an engine position into god-view text or JSON for the CLI."""

from poker_harness.equity import equity
from poker_harness.spot import Spot

PREFLOP_EQUITY_ITERS = 5_000


def god_view(spot: Spot, eng, state, with_equity: bool = True, names: list = None) -> dict:
    """Everything about the position, including every hole card and the
    undealt board. `spot` should already include all actions applied.
    `names` optionally gives a bot id per seat."""
    positions = eng.positions()
    complete  = state["type"] == "hand_complete"
    dealt     = [str(c) for c in eng.community_cards]

    seats = []
    for p in eng.players:
        seats.append({
            "seat":   p.seat,
            "pos":    positions.get(p.seat),
            "stack":  p.stack,
            "bet":    p.bet_this_street,
            "state":  p.state,
            "cards":  "".join(str(c) for c in p.hole_cards) or None,
            "start_stack": spot.stacks[p.seat],
        })
        if names:
            seats[-1]["bot_id"] = names[p.seat]

    eq = None
    contenders = [s for s in seats if s["state"] in ("active", "all_in")]
    if with_equity and not complete and len(contenders) >= 2:
        r  = equity([s["cards"] for s in contenders], board="".join(dealt),
                    iters=PREFLOP_EQUITY_ITERS, seed=0)
        eq = {"method": r["method"], "samples": r["samples"]}
        for s, pr in zip(contenders, r["players"]):
            s["equity"] = pr["equity"]

    compact = spot.compact()
    view = {
        "spot":        compact.to_dict(),
        "street":      state["street"],
        "pot":         state["pot"],
        "board":       dealt,
        "runout":      eng.board_plan[len(dealt):],
        "rigged":      eng.rigged,
        "seed":        spot.seed,
        "seats":       seats,
        "to_act":      None if complete else state["seat_to_act"],
        "legal":       None if complete else state["legal_actions"],
        "line":        compact.to_dict().get("actions", ""),
        "complete":    complete,
        "equity":      eq,
    }
    if complete:
        view["result"] = {
            "showdown":       state["showdown"],
            "winners":        state["winners"],
            "hand_strengths": state["hand_strengths"],
            "final_stacks":   state["final_stacks"],
            "delta": {f"s{s['seat']}": s["stack"] - s["start_stack"] for s in seats},
        }
    return view


def _chips(n: int) -> str:
    return f"{n:,}"


def _seat(seat: int, pos) -> str:
    return f"s{seat}" + (f" ({pos})" if pos else "")


def legal_text(legal: dict) -> str:
    parts = ["fold"]
    parts.append("check" if legal["can_check"] else f"call {_chips(legal['call_amount'])}")
    if legal["can_raise"]:
        lo, hi = legal["min_raise_to"], legal["max_raise_to"]
        parts.append(f"raise {_chips(lo)}..{_chips(hi)}" if lo < hi else f"all-in {_chips(hi)}")
    return " | ".join(parts)


def god_view_text(view: dict, title: str = None) -> str:
    out = []
    head = [title] if title else []
    head += [view["street"], f"pot {_chips(view['pot'])}"]
    tags = []
    if view["seed"] is not None:
        tags.append(f"seed {view['seed']}")
    if view["rigged"]:
        tags.append("rigged")
    if view["complete"]:
        head.append("hand over")
    else:
        to_act = view["seats"][view["to_act"]]
        head.append(f"{_seat(to_act['seat'], to_act['pos'])} to act")
    out.append(" · ".join(head) + (f"   [{', '.join(tags)}]" if tags else ""))

    board = " ".join(view["board"]) or "-"
    runout = " ".join(view["runout"])
    out.append(f"board  {board}" + (f"   (runout: {runout})" if runout else ""))

    has_eq = any("equity" in s for s in view["seats"])
    bot_w  = max([len(s.get("bot_id", "")) for s in view["seats"]] + [0])
    bot_w  = bot_w + 2 if bot_w else 0
    out.append("")
    hdr = (f"   {'seat':<5}" + (f"{'bot':<{bot_w}}" if bot_w else "")
           + f"{'pos':<6}{'stack':>8}{'bet':>7}  {'state':<7} cards")
    out.append(hdr + ("  equity" if has_eq else ""))
    for s in view["seats"]:
        mark  = ">" if s["seat"] == view["to_act"] else " "
        cards = s["cards"] or "-"
        if s["state"] == "folded":
            cards = f"({cards})"
        bot   = f"{s.get('bot_id', ''):<{bot_w}}" if bot_w else ""
        line = (f" {mark} {'s' + str(s['seat']):<5}{bot}{s['pos'] or '-':<6}"
                f"{_chips(s['stack']):>8}{_chips(s['bet']) if s['bet'] else '-':>7}"
                f"  {s['state']:<7} {cards:<6}")
        if "equity" in s:
            line += f"  {s['equity'] * 100:5.1f}%"
        out.append(line.rstrip())
    out.append("")

    if view["line"]:
        out.append(f"line   {view['line']}")
    if view["legal"]:
        out.append(f"legal  {legal_text(view['legal'])}")
    if view["equity"] and view["equity"]["method"] != "exact":
        out.append(f"equity monte carlo, {view['equity']['samples']:,} samples")

    if view["complete"]:
        r = view["result"]
        kind = "showdown" if r["showdown"] else "uncontested"
        out.append(f"result {kind}")
        for w in r["winners"]:
            s = view["seats"][w["seat"]]
            hand = r["hand_strengths"].get(f"s{w['seat']}")
            out.append(f"  {_seat(w['seat'], s['pos'])} wins {_chips(w['amount'])}"
                       + (f" ({w['pot_type']} pot, {hand})" if hand else ""))
        deltas = ", ".join(f"{k} {v:+,}" for k, v in r["delta"].items() if v)
        if deltas:
            out.append(f"  net: {deltas}")
    return "\n".join(out)
