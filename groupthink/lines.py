"""Template library for the scripted debate. Data only: debate.py picks and fills.

Rules every template follows (tests enforce them):
- No digit, no "$", no place and no title literal. Everything concrete is a placeholder:
  {name} {title} {over} {cap} {limit} {qty} {expires} {pct} (advocates) and
  {count} {titles} {skipped} (scout and referee). Money placeholders are filled via money.fmt.
- Templates with {over} or {cap} are only eligible for members who ticked "say my numbers".
- No ratings, popularity or sold counts, no "reserve" words, no dashes (commas instead).
"""

PERSONALITIES = ["blunt", "cheerful", "nerdy", "chill"]

ADVOCATE = {
    # Over this member's budget. Pools mix coarse lines (no numbers) and lines with {over} / {cap}.
    "over_budget": {
        "blunt": [
            '"{title}" is over my budget. Hard pass unless the price drops.',
            'Too pricey for me: "{title}". I need something cheaper.',
            'No to "{title}". It costs more than I can spend.',
            'This is not happening on my budget: "{title}".',
            '"{title}" costs me {over} more than my {cap} cap. Pass.',
            'My cap is {cap}. "{title}" busts it by {over}.',
            '{cap} is my limit and "{title}" is {over} over it. No.',
        ],
        "cheerful": [
            'I like the vibe of "{title}", but it is over my budget!',
            '"{title}" looks fun, though it is over what I can spend. Anything cheaper?',
            'Tempting, "{title}"! It is over my budget, sadly.',
            'Sadly "{title}" is over what I can spend!',
            'Love "{title}", but my cap is {cap} and it runs {over} over. Can we trim it?',
            'Tempting! "{title}" is {over} above my {cap} budget.',
            'My budget tops out at {cap}, and "{title}" is {over} over that. Sad face!',
        ],
        "nerdy": [
            'By my numbers, "{title}" lands above my spending limit. Can we find a cheaper one?',
            'Running the math on "{title}": it exceeds my budget. I would need a lower price.',
            'The budget check on "{title}" fails for me, so I would vote for something cheaper.',
            'My cap is {cap}; "{title}" exceeds it by {over}. That is a hard limit for me.',
            'Delta check: "{title}" minus my {cap} cap leaves {over} uncovered.',
            'Inequality failed: my {cap} limit is {over} short of "{title}".',
        ],
        "chill": [
            '"{title}" is more than I wanted to spend. Could we go cheaper?',
            'Not sure about "{title}", it is above my budget. No stress, just saying.',
            'Honestly "{title}" is past what I planned to spend. Something cheaper works for me.',
            'Fair warning: "{title}" is over my budget.',
            'My limit is {cap}, and "{title}" is {over} over. Maybe something cheaper?',
            '"{title}" runs {over} past my {cap}. I would rather skip it.',
            'Eh, {cap} is where I stop, and "{title}" goes {over} past that.',
        ],
    },
    # Fine print says limit N per person while the group needs more vouchers than that.
    "limit_per_person": {
        "blunt": [
            '"{title}" has a limit of {limit} per person and we need {qty}. That means separate checkouts.',
            'Fine print on "{title}": limit {limit} per person. We need {qty}, so extra checkouts.',
            'Problem with "{title}": it caps at {limit} per person, and we need {qty}.',
        ],
        "cheerful": [
            'Fun pick, "{title}"! Just know the limit is {limit} per person and we need {qty}, so a few checkouts.',
            'I like "{title}", but the fine print says limit {limit} per person. We need {qty}, so we split up the buying!',
            'Heads up on "{title}": limit {limit} per person, {qty} needed. A little teamwork at checkout!',
        ],
        "nerdy": [
            'Constraint found on "{title}": limit {limit} per person against {qty} vouchers needed. Several checkouts.',
            'The fine print for "{title}" says limit {limit} per person. Our need is {qty}, so one checkout is not enough.',
            'For "{title}", {qty} vouchers exceeds the per person limit of {limit}. Plan on separate checkouts.',
        ],
        "chill": [
            'Small snag with "{title}": limit {limit} per person and we need {qty}. A few checkouts then.',
            '"{title}" says limit {limit} per person. We need {qty}, so we would buy it in pieces.',
            'Just noting that "{title}" caps at {limit} per person and we want {qty}. Doable, just fiddly.',
        ],
    },
    # Fine print says an appointment is required.
    "appointment": {
        "blunt": [
            '"{title}" needs an appointment with the business after we buy. Someone has to set it up.',
            'Fine print on "{title}" says appointment required. We schedule it ourselves.',
            'Appointment required for "{title}". The voucher does not set a time for us.',
        ],
        "cheerful": [
            'Quick note on "{title}": we set up a time with the business after buying. Easy enough!',
            '"{title}" looks great! Just remember it needs an appointment with the business afterwards.',
            'For "{title}" we pick a time with the business once we buy. Easy to plan together!',
        ],
        "nerdy": [
            'The fine print for "{title}" requires an appointment, so the date we pick is only a plan.',
            'Detail on "{title}": appointment required, scheduled with the business after purchase.',
            'Note on "{title}": the voucher is not a time slot. The business sets one with us afterwards.',
        ],
        "chill": [
            '"{title}" needs an appointment, so we sort out a time with the business after buying.',
            'Just so we know, "{title}" says appointment required. We contact them after we buy.',
            'With "{title}" we arrange the actual time later, since it needs an appointment.',
        ],
    },
    # Party size is open ended (up to, at least, or not stated).
    "may_cover_more": {
        "blunt": [
            'The listing for "{title}" does not pin down how many people one voucher covers. Check before buying {qty}.',
            '"{title}" may cover more people per voucher than we assume. Do not buy {qty} blind.',
            'Unclear party size on "{title}". We could end up buying more than we need.',
        ],
        "cheerful": [
            'Good news and a question on "{title}": one voucher might cover more of us than expected! Worth checking.',
            '"{title}" does not say exactly how many people a voucher covers. Let us check before we buy {qty}!',
            'Is {qty} the right number of vouchers for "{title}"? The party size is open ended. Worth a look!',
        ],
        "nerdy": [
            'The party size on "{title}" is open ended, so the right quantity is not certain. {qty} is only an estimate.',
            'The listing for "{title}" gives no exact number of people per voucher. Verify before buying {qty}.',
            'Uncertainty on "{title}": the people per voucher is open ended, so {qty} is only an estimate.',
        ],
        "chill": [
            '"{title}" is vague on how many people a voucher covers. Worth checking before buying {qty}.',
            'Not sure how many each voucher covers on "{title}". We should look before buying {qty}.',
            'The party size for "{title}" is loose, so {qty} vouchers is only a guess.',
        ],
    },
    # Something on this member's own wish list matches the offer.
    "matches_like": {
        "blunt": [
            '"{title}" matches something I asked for. I am in.',
            'Good match for me: "{title}". It hits my list.',
            '"{title}" ticks a box on my list. Yes from me.',
        ],
        "cheerful": [
            'Ooh, "{title}" matches something I wanted! Count me in!',
            'Yes! "{title}" fits what I was hoping for!',
            '"{title}" lines up with my wish list. I am excited about this one!',
        ],
        "nerdy": [
            '"{title}" matches an item on my list. That is a point in its favor.',
            'Match found: "{title}" against my stated interests. I support it.',
            'My interests and "{title}" overlap. I count this as a fit for me.',
        ],
        "chill": [
            '"{title}" is right up my alley, it matches something I asked for.',
            'I would be happy with "{title}", it fits what I like.',
            '"{title}" hits something on my list. Works for me.',
        ],
    },
    # Biggest markdown on the shortlist (Ranked.discount_pct).
    "best_discount": {
        "blunt": [
            '"{title}" has the biggest markdown on this list at {pct}% off the listed price.',
            'Price check: "{title}" is {pct}% off its listed price, the biggest cut here.',
            'If we care about price, "{title}" at {pct}% off is the biggest cut on the table.',
        ],
        "cheerful": [
            'Look at the deal on "{title}": {pct}% off the listed price, the biggest cut here!',
            '"{title}" has the biggest markdown of the bunch, {pct}% off! Nice for everyone\'s wallet!',
            'Savings alert! "{title}" is {pct}% off the listed price!',
        ],
        "nerdy": [
            'Of the options on the table, "{title}" has the largest markdown at {pct}% off the listed price.',
            'Ranking by discount, "{title}" comes first at {pct}% off.',
            'Discount comparison: "{title}" leads at {pct}% off the listed price.',
        ],
        "chill": [
            '"{title}" has the biggest markdown here, {pct}% off the listed price. Just saying.',
            'If we like saving money, "{title}" is {pct}% off. Nice perk.',
            'The cut on "{title}" is {pct}% off the listed price, the biggest of these.',
        ],
    },
    # Voucher expires within 60 days (voucherExpires).
    "voucher_soon": {
        "blunt": [
            'The voucher for "{title}" runs out on {expires}. Decide soon.',
            '"{title}" has a voucher that expires {expires}. We need to move.',
            'Clock is ticking on "{title}": the voucher ends {expires}.',
        ],
        "cheerful": [
            'Fun fact on "{title}": the voucher runs out on {expires}, so a plan soon would be great!',
            '"{title}" is sweet, and the voucher is good until {expires}. Let us lock a plan!',
            'Voucher for "{title}" expires {expires}, so let us pick a day soon!',
        ],
        "nerdy": [
            'The voucher for "{title}" has an expiry of {expires}. That is a deadline for our plan.',
            'Constraint on "{title}": use the voucher by {expires}.',
            'Timing note: the voucher for "{title}" expires {expires}, so the window is limited.',
        ],
        "chill": [
            'Only thing to watch with "{title}" is that the voucher runs out on {expires}.',
            'The voucher for "{title}" is good until {expires}, so not forever.',
            '"{title}" works, we just need to use the voucher before {expires}.',
        ],
    },
    # Fallback: the offer fits this member's budget and nothing else stands out.
    "fits_ok": {
        "blunt": [
            '"{title}" fits my budget. No complaints from me.',
            'I can afford "{title}". I am fine with it.',
            '"{title}" works for my budget. Moving on.',
        ],
        "cheerful": [
            '"{title}" fits my budget, so I am happy with it!',
            'Good news, "{title}" works for my wallet!',
            'I am comfortable with "{title}" price wise. Count me in!',
        ],
        "nerdy": [
            'Budget check passed on "{title}". It sits within my limit.',
            '"{title}" satisfies my spending limit. No objection.',
            'My budget constraint is met by "{title}". I am fine with it.',
        ],
        "chill": [
            '"{title}" is within my budget, so I am good with it.',
            'Price is fine for me on "{title}". Works.',
            '"{title}" does not break my budget. I am easy.',
        ],
    },
}

# Scout. {count} real offers on the shortlist, {titles} = "1) A; 2) B; ...".
SCOUT_NONE = [
    "I searched live and found no offers I could put on the table for this group.",
    "Nothing made it through my search for this group. Nothing to argue about yet.",
    "My live search came back with no offers to argue over this time.",
]
SCOUT_ONE = [
    "I searched live and put one real offer on the shortlist: {titles}.",
    "One real offer made the shortlist: {titles}.",
    "Here is the one offer on the shortlist: {titles}.",
]
SCOUT_MANY = [
    "I searched live and put {count} real offers on the shortlist: {titles}.",
    "Shortlist time! {count} real offers made the cut: {titles}.",
    "Here is the shortlist, {count} real offers: {titles}.",
]
# Scout skip line: counts only, never which member or which hard no.
SKIP_ZERO = [
    "I did not have to skip anything for hard no's or the group's needs.",
    "No offers were skipped for hard no's or the group's needs.",
    "Nothing clashed with a hard no or the group's needs, so I skipped nothing.",
]
SKIP_ONE = [
    "I skipped {skipped} offer that clashed with someone's hard no's or the group's needs.",
    "{skipped} offer got skipped because it clashed with a hard no or the group's needs.",
    "Skipped {skipped} offer that did not fit someone's hard no's or the group's needs.",
]
SKIP_MANY = [
    "I skipped {skipped} offers that clashed with someone's hard no's or the group's needs.",
    "{skipped} offers got skipped because they clashed with a hard no or the group's needs.",
    "Skipped {skipped} offers that did not fit someone's hard no's or the group's needs.",
]

# Referee. The pick is code's choice (RankResult.top[0]); the referee only announces it.
REFEREE_PICK_ONE = [
    'My pick: "{title}". It fits every budget in the group. Host, it is your decision.',
    'I land on "{title}". It works for everyone\'s budget. Over to the host.',
    'Verdict: "{title}". Every budget in the group can handle it. Host, take a look.',
]
REFEREE_PICK_MANY = [
    'My pick: "{title}". {fit} fit every budget, so the host can choose another.',
    'I land on "{title}". It works for everyone\'s budget. {fit} do, so the host decides.',
    'Verdict: "{title}" first. {fit} fit everyone\'s budget, host, you pick.',
]
# emptied_by: name the constraint in plain words and say only what the app can actually do: the host can
# relax the budget, the member who set a hard no can drop it. Age and quantity cannot be relaxed. Never auto-relax.
REFEREE_EMPTY = {
    "budget": [
        "Nothing fits everyone's budget right now. The host can relax the budget and I will rerun.",
        "No offer works for every budget in the group. The host can relax it and I will go again.",
        "Everyone's budget together rules out all of these. The host can relax it, then I rerun.",
    ],
    "hard_no": [
        "Hard no's ruled out all I found. The member who set that hard no can drop it, then the host can rerun.",
        "A hard no cleared the whole list. The member who set that hard no can drop it, then the host can rerun.",
        "Everything I found ran into a hard no. The member who set that hard no can drop it, then the host can rerun.",
    ],
    "kids_21": [
        "Age-restricted offers were left out so the plan suits everyone. Try another city or theme.",
        "I skipped age-restricted offers so the plan suits everyone, and none remain. Try another city or theme.",
        "Age-restricted offers were cut so the plan suits everyone. Nothing is left, so try another city or theme.",
    ],
    "quantity": [
        "Most offers lack enough vouchers for the group and none are left. Try another city or theme.",
        "Too few vouchers are available on most offers, and none are left. Try another city or theme.",
        "Most offers do not have enough vouchers for everyone, so none are left. Try another city or theme.",
    ],
    "other": [
        "Nothing fit the group's needs this time. The host can relax a constraint and I will rerun.",
        "No offer made it through the group's needs. The host can relax something and I go again.",
        "The group's needs ruled out most of what I found, and nothing is left. The host can relax one, then I rerun.",
    ],
}
REFEREE_NONE = [
    "Nothing came back for this search. The host can widen it and I will rerun.",
    "There is nothing to pick from yet. The host can widen the search and I go again.",
    "My search found nothing for this group. The host can widen it, then I rerun.",
]

# --- Solo mode: one person's AI arguing with itself. Every line is second person ("you", "your") and
# never says group, vote, host, everyone, someone or the member's name. Two voices share the debate:
# the Saver (money facts) and the Fun side (wish list and the experience itself). {category} is the
# offer's first category label as the API returned it; "experience_plain" is the no-category fallback.
SOLO_ADVOCATE = {
    "saver": {
        "over_budget": [
            '"{title}" is over your budget. Hard pass unless the price drops.',
            'Too pricey for you: "{title}". Look for something cheaper.',
            'Careful, "{title}" costs more than you wanted to spend.',
            '"{title}" is {over} past your {cap} cap. I would skip it.',
            'Your cap is {cap} and "{title}" busts it by {over}.',
            '{cap} is your limit, and "{title}" runs {over} over it.',
        ],
        "best_discount": [
            '"{title}" has the biggest markdown here, {pct}% off the listed price. Your wallet likes that.',
            'Price check: "{title}" is {pct}% off its listed price, the biggest cut on the list.',
            'If you care about price, "{title}" at {pct}% off is the biggest cut here.',
        ],
        "voucher_soon": [
            'The voucher for "{title}" runs out on {expires}. Decide soon.',
            '"{title}" has a voucher that expires {expires}. Your window is short.',
            'Clock is ticking on "{title}": the voucher ends {expires}.',
        ],
        "fits_ok": [
            '"{title}" fits your budget. No complaints from me.',
            'You can afford "{title}". It is a safe choice.',
            '"{title}" does not break your budget. I am fine with it.',
        ],
    },
    "fun": {
        "matches_like": [
            '"{title}" matches something you asked for. That is a big plus.',
            'Good match for you: "{title}". It hits your list.',
            '"{title}" ticks a box on your list. Go for it.',
        ],
        "experience": [
            '"{title}" is listed under {category}. Worth a look just for the fun of it.',
            'Think of "{title}", filed under {category}. That is a treat for you.',
            'Fun pick: "{title}", under {category}. Make it a day you remember.',
        ],
        "experience_plain": [
            '"{title}" is the fun pick for you. Try something new.',
            'Think of "{title}". It is a treat you can look forward to.',
            'Fun pick: "{title}". Make it a day you remember.',
        ],
    },
}

SOLO_SCOUT_NONE = [
    "I searched live and found no offers to put in front of you.",
    "Nothing made it through my search for you. Nothing to argue about yet.",
    "My live search came back with no offers for you this time.",
]
SOLO_SCOUT_ONE = [
    "I searched live and put one real offer in front of you: {titles}.",
    "One real offer made your shortlist: {titles}.",
    "Here is the one offer on your shortlist: {titles}.",
]
SOLO_SCOUT_MANY = [
    "I searched live and put {count} real offers in front of you: {titles}.",
    "Shortlist time! {count} real offers made the cut for you: {titles}.",
    "Here is your shortlist, {count} real offers: {titles}.",
]
SOLO_SKIP_ZERO = [
    "I did not have to skip anything for your hard no's or your needs.",
    "No offers were skipped for your hard no's or your needs.",
    "Nothing clashed with your hard no's or your needs, so I skipped nothing.",
]
SOLO_SKIP_ONE = [
    "I skipped {skipped} offer that clashed with your hard no's or your needs.",
    "{skipped} offer got skipped because it clashed with your hard no's or your needs.",
    "Skipped {skipped} offer that did not fit your hard no's or your needs.",
]
SOLO_SKIP_MANY = [
    "I skipped {skipped} offers that clashed with your hard no's or your needs.",
    "{skipped} offers got skipped because they clashed with your hard no's or your needs.",
    "Skipped {skipped} offers that did not fit your hard no's or your needs.",
]
SOLO_REFEREE_PICK_ONE = [
    'My pick for you: "{title}". It fits your budget. The choice is yours.',
    'I land on "{title}". It works for your budget. Over to you.',
    'Verdict: "{title}". Your budget can handle it. Take a look.',
]
SOLO_REFEREE_PICK_MANY = [
    'My pick for you: "{title}". {fit} fit your budget, so you can choose another.',
    'I land on "{title}". It works for your budget. {fit} do, so you decide.',
    'Verdict: "{title}" first. {fit} fit your budget, you pick.',
]
SOLO_REFEREE_EMPTY = {
    "budget": [
        "Nothing fits your budget right now. Relax it and I will rerun.",
        "No offer works for your budget. Relax it and I will go again.",
        "Your budget rules out all of these. Relax it, then I rerun.",
    ],
    "hard_no": [
        "Your hard no's ruled out all I found. Drop one and I will rerun.",
        "A hard no cleared the whole list. Drop it and I will rerun.",
        "Everything I found ran into a hard no. Drop one and I will go again.",
    ],
    "kids_21": [
        "Age-restricted offers were left out and none remain. Try another city or theme.",
        "I skipped age-restricted offers, and none remain. Try another city or theme.",
        "Age-restricted offers were cut. Nothing is left, so try another city or theme.",
    ],
    "quantity": [
        "Most offers lack enough vouchers and none are left. Try another city or theme.",
        "Too few vouchers are available on most offers, and none are left. Try another city or theme.",
        "Most offers do not have enough vouchers for you, so none are left. Try another city or theme.",
    ],
    "other": [
        "Nothing fit your needs this time. Relax one and I will rerun.",
        "No offer made it through your needs. Relax something and I go again.",
        "Your needs ruled out most of what I found, and nothing is left. Relax one, then I rerun.",
    ],
}
SOLO_REFEREE_NONE = [
    "Nothing came back for this search. Widen it and I will rerun.",
    "There is nothing to pick from yet. Widen your search and I go again.",
    "My search found nothing for you. Widen it, then I rerun.",
]
