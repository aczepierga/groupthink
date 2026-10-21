"""Demo plan data: the visitor's default card and two sample people. Data only, no logic.

Sample PEOPLE are allowed because they are labelled (every name starts with SAMPLE_PREFIX, so the
label survives into debate lines, member lists and result pages). The offers a demo plan argues over
are always the live ones from the API; nothing here is an offer.
"""

SAMPLE_PREFIX = "Sample: "
DATE_TEXT = "This weekend"
THEME = "fun night out"
GROUP_SIZE = 3

# The visitor: host of the demo plan and a member, so they can open and edit their own card.
YOU = {"name": "You", "budget_minor": 6000, "likes": [], "hard_nos": [], "kids": False, "constraints": "",
       "share_numbers": True, "sample": False}

SAMPLES = [
    {"name": SAMPLE_PREFIX + "Ana", "budget_minor": 4500, "likes": ["food", "drinks"],
     "hard_nos": ["heights"], "kids": False, "constraints": "", "share_numbers": True, "sample": True},
    {"name": SAMPLE_PREFIX + "Ben", "budget_minor": 8000, "likes": ["games", "active"],
     "hard_nos": [], "kids": False, "constraints": "", "share_numbers": False, "sample": True},
]
