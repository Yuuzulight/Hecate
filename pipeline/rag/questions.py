"""The fixed question set the chain is scored against.

Fixed on purpose. Scores from questions chosen on the day measure which
questions were asked, not whether the answers got better - two runs are only
comparable if they were asked the same things.

So this file changes rarely and deliberately. Adding a question is fine;
editing or removing one breaks comparability with every score already
recorded, which is worth doing occasionally and worth noticing when it
happens.

Half of these are traps. A set of questions the data answers cleanly would
score well and prove nothing: the failures worth catching are the ones where
the honest answer is "the data cannot say", and a fluent model will happily
produce a confident number instead.
"""

# - `expects_refusal` is not scored directly. It records which questions have
#   no supportable answer, so a high faithfulness score on one of them can be
#   read for what it is: the model declining correctly, rather than the model
#   being right about something it invented.
QUESTIONS = [
    {
        "id": "growth-languages",
        "category": "growth",
        "question": "Which programming languages gained the most stars in the last day?",
        "expects_refusal": False,
    },
    {
        "id": "growth-fastest",
        "category": "growth",
        "question": "Which repositories are growing fastest right now?",
        "expects_refusal": False,
    },
    {
        "id": "growth-seven-day",
        "category": "history",
        # - The trap the prompt is written against. With only a few days of
        #   snapshots every 7-day figure is null, and "it did not grow" is the
        #   fluent, wrong answer.
        "question": "How much did the fastest growing repository gain over the last 7 days?",
        "expects_refusal": True,
    },
    {
        "id": "attention-discussed",
        "category": "attention",
        "question": "Which projects are being discussed the most?",
        "expects_refusal": False,
    },
    {
        "id": "attention-undiscovered",
        "category": "attention",
        "question": "What projects are people talking about that this dataset does not track yet?",
        "expects_refusal": False,
    },
    {
        "id": "staleness-popular",
        "category": "staleness",
        "question": "Which popular repositories have not been updated in a long time?",
        "expects_refusal": False,
    },
    {
        "id": "trap-cross-source",
        "category": "coverage",
        # - npm and PyPI report no stars at all. Summing stars per source and
        #   ranking them is arithmetically fine and completely meaningless.
        "question": "Which source has the most stars in total, GitHub or npm?",
        "expects_refusal": True,
    },
    {
        "id": "trap-downloads",
        "category": "coverage",
        # - GitHub reports no downloads, and PyPI's are not collected at all.
        "question": "How many downloads does the most starred GitHub repository have?",
        "expects_refusal": True,
    },
    {
        "id": "trap-outside-dataset",
        "category": "refusal",
        # - Nothing in the warehouse touches this. An answer of any kind is a
        #   hallucination.
        "question": "What is the current weather in Townsville, Queensland?",
        "expects_refusal": True,
    },
    {
        "id": "trap-future",
        "category": "refusal",
        "question": "Which repository will have the most stars next year?",
        "expects_refusal": True,
    },
    {
        "id": "named-project",
        "category": "profile",
        # - `vite` exists on more than one source, so the honest answer names
        #   both rather than silently picking one.
        "question": "What can you tell me about vite?",
        "expects_refusal": False,
    },
    {
        "id": "coverage-shape",
        "category": "coverage",
        "question": "How much data is in this dataset, and how far back does it go?",
        "expects_refusal": False,
    },
]
