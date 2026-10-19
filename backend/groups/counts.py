"""Correlated count annotations for ``Group`` querysets (#1766).

``Count("memberships", distinct=True)`` next to ``Count("boards", distinct=True)``
and ``Count("subgroups", distinct=True)`` joins members x boards x subgroups per
group before DISTINCT collapses them, so latency scaled with the product on large
groups while the query count stayed flat. Each count is now an independent
correlated subquery (same approach as ``boards.views._helpers.annotate_board_counts``,
#1723). Field names and types are unchanged: non-negative ints, 0 never null.

Value semantics: the old counts were annotated *before* any filter and the only
filters applied afterwards (``id__in`` access scoping, ``favorites__user``) are
row-preserving for one user, so the old distinct counts were already true totals;
the subquery counts return the same numbers.
"""
from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce


def _count(rows, key):
    return Coalesce(
        Subquery(
            rows.order_by().values(key).annotate(c=Count("*")).values("c"),
            output_field=IntegerField(),
        ),
        0,
        output_field=IntegerField(),
    )


def annotate_group_counts(qs):
    """Annotate ``_member_count`` / ``_board_count`` / ``_subgroup_count``."""
    from boards.models import Board

    from .models import Group, GroupMembership

    return qs.annotate(
        _member_count=_count(
            GroupMembership.objects.filter(group=OuterRef("pk")), "group"
        ),
        _board_count=_count(Board.objects.filter(group=OuterRef("pk")), "group"),
        _subgroup_count=_count(
            Group.objects.filter(parent=OuterRef("pk")), "parent"
        ),
    )
