import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import gemini_delegate
import gemini_fanout


class GeminiDelegateCliTestCase(unittest.TestCase):
    def test_build_parser_when_model_not_provided_uses_flash_default(self) -> None:
        parser = gemini_delegate.build_parser()

        args = parser.parse_args([])

        self.assertEqual(args.model, gemini_delegate.DEFAULT_MODEL)
        self.assertEqual(gemini_delegate.DEFAULT_MODEL, "gemini-3-flash-preview")

    @patch("gemini_delegate._run_gemini_with_retries")
    def test_main_when_mode_is_research_uses_research_prompt(self, mock_run) -> None:
        mock_run.return_value = (0, "helper output\n", "")

        with patch.object(sys, "argv", ["gemini_delegate.py", "--mode", "research"]):
            with patch("sys.stdin", io.StringIO("TASK: Investigate the API behavior.")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = gemini_delegate.main()

        self.assertEqual(rc, 0)
        self.assertEqual(mock_run.call_args.kwargs["query"], gemini_delegate.QUERY_RESEARCH)

    @patch("gemini_delegate._run_gemini_with_retries")
    def test_main_when_mode_is_answer_uses_answer_prompt(self, mock_run) -> None:
        mock_run.return_value = (0, "short answer\n", "")

        with patch.object(sys, "argv", ["gemini_delegate.py", "--mode", "answer"]):
            with patch("sys.stdin", io.StringIO("TASK: Explain the likely root cause.")):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = gemini_delegate.main()

        self.assertEqual(rc, 0)
        self.assertEqual(mock_run.call_args.kwargs["query"], gemini_delegate.QUERY_ANSWER)


class GeminiFanoutCliTestCase(unittest.TestCase):
    def test_build_parser_when_model_not_provided_uses_flash_default(self) -> None:
        parser = gemini_fanout.build_parser()

        args = parser.parse_args([])

        self.assertEqual(args.model, gemini_fanout.DEFAULT_MODEL)
        self.assertEqual(gemini_fanout.DEFAULT_MODEL, "gemini-3-flash-preview")

    def test_build_parser_when_jobs_include_research_accepts_it(self) -> None:
        parser = gemini_fanout.build_parser()

        args = parser.parse_args(["--jobs", "review", "research"])

        self.assertEqual(args.jobs, ["review", "research"])

    def test_build_parser_when_jobs_include_answer_accepts_it(self) -> None:
        parser = gemini_fanout.build_parser()

        args = parser.parse_args(["--jobs", "review", "answer"])

        self.assertEqual(args.jobs, ["review", "answer"])


if __name__ == "__main__":
    unittest.main()
