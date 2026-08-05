"""Tests for hierarchical category paths."""

from __future__ import annotations

import pytest

from minmax.core import paths


class TestNormalize:
    @pytest.mark.parametrize(
        "raw",
        [
            "media/gaming/chess",
            " media/gaming/chess ",
            "media / gaming / chess",
            "media//gaming///chess",
            "/media/gaming/chess/",
        ],
    )
    def test_variants_collapse_to_one_canonical_path(self, raw: str) -> None:
        # Without this, a stray space in a source file would silently create a
        # second category and split that activity's history in two.
        assert paths.normalize(raw) == "media/gaming/chess"

    @pytest.mark.parametrize("raw", ["", "   ", "/", "///", " / / "])
    def test_paths_with_no_real_components_are_rejected(self, raw: str) -> None:
        with pytest.raises(ValueError, match="no non-empty components"):
            paths.normalize(raw)

    def test_internal_spaces_are_preserved(self) -> None:
        # Real categories contain spaces: 'research/lit review', 'school/exam/CSE 351'.
        assert paths.normalize("research/lit review") == "research/lit review"


class TestAccessors:
    def test_leaf_is_the_display_name(self) -> None:
        assert paths.leaf("media/gaming/chess") == "chess"
        assert paths.leaf("admin") == "admin"

    def test_top_level_is_the_coloring_group(self) -> None:
        assert paths.top_level("media/gaming/chess") == "media"
        assert paths.top_level("admin") == "admin"

    def test_ancestors_walk_outermost_first(self) -> None:
        assert paths.ancestors("media/gaming/chess") == [
            "media",
            "media/gaming",
            "media/gaming/chess",
        ]

    def test_ancestors_can_exclude_the_path_itself(self) -> None:
        assert paths.ancestors("media/gaming/chess", include_self=False) == [
            "media",
            "media/gaming",
        ]
        assert paths.ancestors("admin", include_self=False) == []


class TestIsDescendant:
    def test_matches_a_path_and_its_descendants(self) -> None:
        assert paths.is_descendant("media/gaming/chess", "media")
        assert paths.is_descendant("media/gaming/chess", "media/gaming")
        assert paths.is_descendant("media", "media")

    def test_compares_components_not_characters(self) -> None:
        # The bug a naive startswith() would introduce.
        assert not paths.is_descendant("mediation/notes", "media")
        assert not paths.is_descendant("media-old/x", "media")

    def test_a_prefix_is_not_a_descendant_of_its_own_child(self) -> None:
        assert not paths.is_descendant("media", "media/gaming")

    def test_include_self_can_be_disabled(self) -> None:
        assert not paths.is_descendant("media", "media", include_self=False)
        assert paths.is_descendant("media/gaming", "media", include_self=False)
