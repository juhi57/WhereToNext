"""The command-line interface.

The encoding tests here cover a real bug: a default Windows console is cp1252,
which has no rupee glyph, so printing a formatted INR amount raised
``UnicodeEncodeError`` and killed the command. The CLI now degrades to the same
ASCII-safe format the PDF uses.
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import cli
from app.models.enums import Currency, Interest, Pace, StayType, TransportMode


def future(days_ahead: int = 45) -> str:
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def base_args(*extra: str) -> list[str]:
    return [
        "--from",
        "Mumbai",
        "--to",
        "Bali, Indonesia",
        "--start",
        future(),
        "--days",
        "6",
        "--budget",
        "180000",
        *extra,
    ]


class TestArgumentParsing:
    def test_days_and_end_are_equivalent(self) -> None:
        parser = cli._build_parser()
        start = future()
        end = (date.fromisoformat(start) + timedelta(days=5)).isoformat()

        by_days = cli._request_from(parser.parse_args(["estimate", *base_args()]))
        by_end = cli._request_from(
            parser.parse_args(
                [
                    "estimate",
                    "--from",
                    "Mumbai",
                    "--to",
                    "Bali, Indonesia",
                    "--start",
                    start,
                    "--end",
                    end,
                    "--budget",
                    "180000",
                ]
            )
        )
        assert by_days.end_date == by_end.end_date
        assert by_days.duration_days == 6

    def test_interests_are_parsed_from_a_comma_list(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(["estimate", *base_args("--interests", "beaches, food ,culture")])
        request = cli._request_from(args)
        assert request.interests == [Interest.BEACHES, Interest.FOOD, Interest.CULTURE]

    def test_empty_interests_yield_an_empty_list(self) -> None:
        parser = cli._build_parser()
        request = cli._request_from(parser.parse_args(["estimate", *base_args()]))
        assert request.interests == []

    def test_all_preferences_reach_the_request(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "estimate",
                *base_args(
                    "--adults",
                    "3",
                    "--children",
                    "2",
                    "--currency",
                    "USD",
                    "--transport",
                    "train",
                    "--stay",
                    "hostel",
                    "--food",
                    "vegan",
                    "--pace",
                    "relaxed",
                    "--notes",
                    "quiet trip",
                    "--accessibility",
                    "step-free",
                ),
            ]
        )
        request = cli._request_from(args)
        assert request.adults == 3
        assert request.children == 2
        assert request.currency is Currency.USD
        assert request.primary_transport() is TransportMode.TRAIN
        assert request.stay_type is StayType.HOSTEL
        assert request.pace is Pace.RELAXED
        assert request.notes == "quiet trip"
        assert request.accessibility_needs == "step-free"

    def test_a_malformed_date_is_a_clear_error(self) -> None:
        parser = cli._build_parser()
        args = parser.parse_args(
            [
                "estimate",
                "--from",
                "A City",
                "--to",
                "B City",
                "--start",
                "13-12-2026",
                "--days",
                "3",
                "--budget",
                "100",
            ]
        )
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            cli._request_from(args)

    def test_days_and_end_are_mutually_exclusive(self) -> None:
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "estimate",
                    "--from",
                    "A",
                    "--to",
                    "B",
                    "--start",
                    future(),
                    "--days",
                    "3",
                    "--end",
                    future(50),
                    "--budget",
                    "100",
                ]
            )

    def test_trip_length_is_required(self) -> None:
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["estimate", "--from", "A", "--to", "B", "--start", future(), "--budget", "100"]
            )


class TestEncodingFallback:
    def test_detects_a_console_that_cannot_render_the_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Cp1252Stream:
            encoding = "cp1252"

        monkeypatch.setattr(cli.sys, "stdout", Cp1252Stream())
        assert cli._stream_supports("₹") is False
        assert cli._stream_supports("plain text") is True

    def test_detects_a_utf8_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Utf8Stream:
            encoding = "utf-8"

        monkeypatch.setattr(cli.sys, "stdout", Utf8Stream())
        assert cli._stream_supports("₹…") is True

    def test_an_unknown_codec_is_treated_as_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class OddStream:
            encoding = "not-a-real-codec"

        monkeypatch.setattr(cli.sys, "stdout", OddStream())
        assert cli._stream_supports("₹") is False

    def test_money_uses_the_ascii_format_when_the_console_is_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "ASCII_ONLY", True)
        rendered = cli._money(125_000, Currency.INR)
        assert rendered == "Rs. 1,25,000"
        rendered.encode("cp1252")  # must not raise

    def test_money_keeps_the_symbol_on_a_capable_console(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "ASCII_ONLY", False)
        assert cli._money(125_000, Currency.INR) == "₹1,25,000"

    def test_truncation_marker_is_ascii_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "ELLIPSIS", "...")
        trimmed = cli._fit("x" * 80, width=20)
        assert len(trimmed) <= 20
        trimmed.encode("cp1252")  # must not raise

    def test_short_labels_are_left_alone(self) -> None:
        assert cli._fit("short", width=20) == "short"

    def test_module_resolves_a_flag_at_import(self) -> None:
        """ASCII_ONLY must exist regardless of the console the suite runs under."""
        reloaded = importlib.reload(cli)
        assert isinstance(reloaded.ASCII_ONLY, bool)


class TestCommands:
    def test_estimate_prints_a_costing(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "ASCII_ONLY", True)
        exit_code = cli.main(["estimate", *base_args()])
        output = capsys.readouterr().out

        assert exit_code == 0  # under budget
        assert "Mumbai to Bali, Indonesia" in output
        assert "TOTAL" in output
        assert "Rs." in output

    def test_estimate_signals_an_over_budget_trip_with_exit_code_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero exit lets the command be used in a shell condition."""
        monkeypatch.setattr(cli, "ASCII_ONLY", True)
        exit_code = cli.main(
            [
                "estimate",
                "--from",
                "Mumbai",
                "--to",
                "Tokyo",
                "--start",
                future(),
                "--days",
                "10",
                "--budget",
                "15000",
            ]
        )
        assert exit_code == 1
        assert "OVER BUDGET" in capsys.readouterr().out

    def test_plan_writes_a_pdf(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli, "ASCII_ONLY", True)
        monkeypatch.setenv("WTN_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("WTN_FORCE_HEURISTIC", "true")
        cli.get_settings.cache_clear()

        target = tmp_path / "nested" / "trip.pdf"
        try:
            exit_code = cli.main(["plan", *base_args("--pdf", str(target))])
        finally:
            cli.get_settings.cache_clear()

        assert exit_code in (0, 1)
        assert target.is_file()
        assert target.read_bytes().startswith(b"%PDF-")
        assert "Day 1" in capsys.readouterr().out

    def test_plan_can_emit_json(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        monkeypatch.setenv("WTN_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("WTN_FORCE_HEURISTIC", "true")
        cli.get_settings.cache_clear()
        try:
            cli.main(["plan", *base_args("--json")])
        finally:
            cli.get_settings.cache_clear()

        payload = json.loads(capsys.readouterr().out)
        assert payload["budget"]["total"] > 0
        assert len(payload["days"]) == 6

    def test_no_subcommand_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main([]) == 2
        assert "usage: wheretonext" in capsys.readouterr().out

    def test_an_invalid_trip_is_reported_cleanly(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A validation failure is an error message, not a traceback."""
        exit_code = cli.main(
            [
                "estimate",
                "--from",
                "Mumbai",
                "--to",
                "Mumbai",
                "--start",
                future(),
                "--days",
                "3",
                "--budget",
                "1000",
            ]
        )
        assert exit_code == 2
        assert "error:" in capsys.readouterr().err
