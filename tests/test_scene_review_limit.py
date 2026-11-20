import unittest

from ai_video.video_higgsfield_jutsu.python_backend.previs_finalization import (
    INITIAL_BUILD_LIMIT,
    SCENE_REVIEW_LIMIT,
    count_scene_review,
)


class SceneReviewLimitTests(unittest.TestCase):
    def test_initial_build_has_separate_three_attempt_limit(self):
        self.assertEqual(INITIAL_BUILD_LIMIT, 3)
        job = {}
        logs = []
        count_scene_review(job, "run_python", "build_scene()", lambda *args: logs.append(args))
        self.assertEqual(job["reviewBudget"]["limit"], 5)
        self.assertEqual(job["reviewBudget"]["used"], 0)
        self.assertIn("1/3", logs[-1][1])

    def test_queries_and_mutations_share_five_reviews(self):
        self.assertEqual(SCENE_REVIEW_LIMIT, 5)
        job = {}
        count_scene_review(job, "run_python", "build_scene()", lambda *args: None)
        job["reviewBudget"]["buildCommitted"] = True
        for index, kind in enumerate(("query_python", "run_python", "get_project", "query_python", "run_python"), 1):
            count_scene_review(job, kind, "update_scene()", lambda *args: None)
            self.assertEqual(job["reviewBudget"]["used"], index)
        self.assertEqual(job["reviewBudget"]["used"], job["reviewBudget"]["limit"])
