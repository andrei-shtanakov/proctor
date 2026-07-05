"""Tests for shared fnmatch-glob heuristics."""

from proctor.core.globs import is_strictly_broader, patterns_overlap


class TestIsStrictlyBroader:
    def test_wildcard_subsumes_literal(self) -> None:
        assert is_strictly_broader("trigger.*", "trigger.terminal")

    def test_equal_patterns_not_strict(self) -> None:
        assert not is_strictly_broader("trigger.terminal", "trigger.terminal")

    def test_narrower_is_not_broader(self) -> None:
        assert not is_strictly_broader("trigger.terminal", "trigger.*")


class TestPatternsOverlap:
    def test_identical_literals(self) -> None:
        assert patterns_overlap("src/main.py", "src/main.py")

    def test_glob_covers_literal(self) -> None:
        assert patterns_overlap("src/**", "src/foo/bar.py")

    def test_literal_under_glob_reversed(self) -> None:
        assert patterns_overlap("src/foo/bar.py", "src/**")

    def test_path_prefix_without_wildcard(self) -> None:
        # fnmatch alone would miss this: "src" does not fnmatch "src/foo.py"
        assert patterns_overlap("src", "src/foo.py")

    def test_disjoint_trees(self) -> None:
        assert not patterns_overlap("src/**", "docs/**")

    def test_disjoint_literals(self) -> None:
        assert not patterns_overlap("src/a.py", "src/b.py")

    def test_sibling_prefix_not_confused(self) -> None:
        # "src" is not a path-prefix of "srcx/foo.py"
        assert not patterns_overlap("src", "srcx/foo.py")
