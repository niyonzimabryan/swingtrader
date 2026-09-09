"""The research workspace (Spec M): dossiers, theses, invalidators, journal.

Layout:

``trust``
    The source-tier vocabulary, and the two independent decisions it drives:
    is this content trusted (Spec P §5), and may it reach the public repo
    (Spec K §3.3)?
``store``
    Every read and write of the six tables. The invalidator discipline of
    Spec M §4 lives here, in code, next to the database CHECK constraints that
    back it up.
``invalidators``
    The five types, their parameter schemas, and the daily check job.
``prices``
    The price seam the ``price_level`` check reads through, so the price plane
    can arrive in a parallel phase without changing this package.
``paging``
    The alert callable a triggered invalidator fires. It pages. It never
    creates an order or a proposal, and nothing here imports ``execution``.
``metrics``
    Honesty metrics, the Brier score with its decomposition, and the
    calibration table — with the small-n floors that make them print
    ``insufficient`` rather than a flattering number.
``mirror``
    Postgres → Markdown, deliberately partial, plus the ``--import`` path.
``offline``
    Reading the mirror with no database and no network, which is the fallback
    Spec K §3.3 designs for.

Nothing in this package imports ``execution``, ``bot``, or any broker adapter;
``tests/test_no_execute_scope.py`` keeps it that way.
"""
