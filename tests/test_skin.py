"""Arcade skin: shared chrome on every page, CSP-clean markup, local assets, logo file, rounds markup."""
import re
import struct
from pathlib import Path

import pytest

from test_cart import DEMO_NOTICE, _cart_env, demo_cart, final_group, get_cart_link, make_env  # noqa: F401
from test_web import INLINE_HANDLER, REQUIRED_COPY, Env, _env, argue, card, group, new_solo  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
FOOTER = "Unofficial contest entry, built on the LivingSocial Partner API."
TOP_LINK = re.compile(r'<a\b[^>]*href="https://www\.groupon\.com/"[^>]*>\s*Deals on groupon\.com\s*</a>')


def pages():
    """name -> html for every kind of page the site serves."""
    out = {}
    env = Env()
    c = env.browser()
    out["home"] = c.get("/").text
    gpid, host, members = group(env)
    out["group_plan"] = host.get(f"/p/{gpid}").text
    out["group_card"] = host.get(f"/p/{gpid}/card").text
    argue(host, gpid)
    out["group_argue"] = host.get(f"/p/{gpid}/argue").text
    sc = env.browser()
    spid = new_solo(sc)
    out["solo_card"] = sc.get(f"/p/{spid}/card").text
    card(sc, spid, "Ann", "50")
    argue(sc, spid)
    out["solo_argue"] = sc.get(f"/p/{spid}/argue").text
    out["thanks"] = c.get("/thanks?grouponOrderUuid=abc").text
    out["error"] = c.post("/plans", data={}).text
    out["expired"] = c.get("/p/gone0000gone0000").text
    cenv, api = make_env()
    pid, chost, _, _ = final_group(cenv, api)
    out["cart"] = get_cart_link(chost, pid).text
    dc, dpid = demo_cart(cenv, api)
    out["demo_cart"] = get_cart_link(dc, dpid).text
    return out


@pytest.fixture(scope="module")
def all_pages():
    # module scope cannot use the function-scoped autouse env fixtures, so set the env by hand
    import os
    from groupthink import config
    saved = {k: os.environ.get(k) for k in ("IP_SALT", "LS_PARTNER_KEY")}
    from test_web import KEY
    os.environ["IP_SALT"] = "test-salt-not-secret"
    os.environ["LS_PARTNER_KEY"] = KEY
    for name in ("PROBES_ENABLED", "KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
                 "UPSTASH_REDIS_REST_TOKEN", "ADMIN_TOKEN"):
        os.environ.pop(name, None)
    config._reset_cache()
    try:
        yield pages()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config._reset_cache()


def test_every_page_has_top_bar_and_footer(all_pages):
    assert len(all_pages) >= 10
    for name, t in all_pages.items():
        assert TOP_LINK.search(t), name
        link = TOP_LINK.search(t).group(0)
        assert 'rel="noopener noreferrer"' in link, name
        assert FOOTER in t, name
        foot = re.search(r"<footer.*?</footer>", t, re.S)
        assert foot, name
        for url in ("https://www.groupon.com/", "https://www.livingsocial.com/"):
            m = re.search(r'<a\b[^>]*href="%s"[^>]*>' % re.escape(url), foot.group(0))
            assert m and 'rel="noopener noreferrer"' in m.group(0), (name, url)


def test_wordmark_sits_in_the_logo_slot(all_pages):
    for name, t in all_pages.items():
        assert re.search(r'<a class="brand"[^>]*>GroupThink</a>', t), name
    base = (ROOT / "templates" / "base.html").read_text()
    assert "{% block logo %}" in base and "{% endblock %}" in base


def test_no_inline_script_style_or_handlers(all_pages):
    for name, t in all_pages.items():
        assert not re.search(r"<script(?![^>]*\bsrc=)", t), name
        assert "<style" not in t.lower() and "style=" not in t.lower(), name
        assert not INLINE_HANDLER.search(t), name
        assert "javascript:" not in t.lower(), name


def test_assets_are_local(all_pages):
    for name, t in all_pages.items():
        for tag in re.findall(r"<(?:script|img|link)\b[^>]*>", t):
            for ref in re.findall(r'\b(?:src|href)="([^"]*)"', tag):
                assert ref.startswith("/static/"), (name, tag)
    css = (ROOT / "static" / "style.css").read_text()
    assert "@import" not in css and "url(http" not in css and "url(//" not in css
    assert "http://" not in css and "https://" not in css


def test_home_has_two_modes_and_demo(all_pages):
    t = all_pages["home"]
    assert "1 PLAYER" in t and "2+ PLAYERS" in t
    assert re.search(r'<form[^>]*action="/solo"', t)
    assert re.search(r'<form[^>]*action="/plans"', t)
    assert re.search(r'<form[^>]*action="/demo"', t)
    ids = re.findall(r'\bid="([^"]*)"', t)
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
    for name, page in all_pages.items():
        pid_list = re.findall(r'\bid="([^"]*)"', page)
        assert len(pid_list) == len(set(pid_list)), (name, [i for i in pid_list if pid_list.count(i) > 1])


def test_money_facts_survive_skin(all_pages):
    for name in ("cart", "demo_cart"):
        t = all_pages[name]
        assert REQUIRED_COPY in t, name
        assert 'class="notice"' in t, name
    assert DEMO_NOTICE in all_pages["demo_cart"]
    assert DEMO_NOTICE not in all_pages["cart"]
    t = all_pages["cart"]
    assert "$30.00" in t and "$90.00" in t and "3 vouchers" in t


def test_logo_png_is_valid():
    p = ROOT / "static" / "logo.png"
    data = p.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (512, 512)
    assert len(data) <= 512000


def test_reduced_motion_rule_present():
    css = (ROOT / "static" / "style.css").read_text()
    assert "prefers-reduced-motion" in css
    block = css[css.index("prefers-reduced-motion"):]
    assert "animation: none" in block or "animation:none" in block


def test_debate_lines_carry_round_numbers_and_referee_ko(all_pages):
    for name in ("group_argue", "solo_argue"):
        t = all_pages[name]
        tpl = re.search(r'<template id="debate-lines">(.*?)</template>', t, re.S).group(1)
        rounds = [int(n) for n in re.findall(r'<li[^>]*\bdata-round="(\d)"', tpl)]
        assert rounds and rounds[0] == 1 and rounds == sorted(rounds) and max(rounds) <= 3, (name, rounds)
        assert re.search(r'<li[^>]*class="line referee"[^>]*>.*?<span class="ko">K\.O\.</span>', tpl, re.S), name
        assert 'data-rounds="%d"' % max(rounds) in t, name


def test_solo_argue_names_both_voices_and_winner_box(all_pages):
    t = all_pages["solo_argue"]
    assert "Your Saver" in t and "Your Fun side" in t
    assert re.search(r'<p class="winner"[^>]*><span class="tag">WINNER</span>', t)


def test_title_screen_markup(all_pages):
    t = all_pages["home"]
    assert 'class="title-screen"' in t
    assert len(re.findall(r'class="mode\b', t)) == 2


def test_round_numbers_follow_the_debate_shape():
    from groupthink.debate import Line
    from groupthink.web import _round_count, _round_numbers

    def ln(who, kind):
        return Line(who, kind, "x", None)

    full = [ln("Scout", "scout"), ln("Scout", "scout"), ln("A", "advocate"), ln("B", "advocate"),
            ln("A", "advocate"), ln("B", "advocate"), ln("Referee", "referee")]
    assert _round_numbers(full) == [1, 1, 2, 2, 3, 3, 3]
    short = [ln("Scout", "scout"), ln("Referee", "referee")]
    assert _round_numbers(short) == [1, 2] and _round_count(short) == 2
    one_round = [ln("Scout", "scout"), ln("A", "advocate"), ln("Referee", "referee")]
    assert _round_numbers(one_round) == [1, 2, 3]
    assert _round_count([]) == 1
