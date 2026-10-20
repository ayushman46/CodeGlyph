"""A chat model NVIDIA stopped serving must not break answers or edits."""

import unittest
from unittest.mock import patch

import config
from config import RETIRED_CHAT_MODELS, get_answer_model_options, settings

RETIRED = "nvidia/nemotron-3-super-120b-a12b"


class RetiredModelRemapTests(unittest.TestCase):
    def test_retired_id_in_the_environment_is_remapped_for_every_profile(self):
        current = RETIRED_CHAT_MODELS[RETIRED]
        with patch.object(settings, "nemotron_model", RETIRED), \
             patch.object(settings, "code_editing_model", RETIRED), \
             patch.object(settings, "detailed_nemotron_model", RETIRED):
            self.assertEqual(get_answer_model_options("fast")[0], current)
            self.assertEqual(get_answer_model_options("detailed")[0], current)
            self.assertEqual(get_answer_model_options("fast", "editing")[0], current)
            self.assertEqual(get_answer_model_options("code")[0], current)

    def test_working_ids_are_left_alone(self):
        with patch.object(settings, "nemotron_model", "nvidia/some-working-model"):
            self.assertEqual(get_answer_model_options("fast")[0], "nvidia/some-working-model")

    def test_defaults_no_longer_name_the_retired_model(self):
        defaults = config.Settings.model_fields
        for field in ("nemotron_model", "detailed_nemotron_model", "code_editing_model", "code_editing_fallback_model"):
            self.assertNotEqual(defaults[field].default, RETIRED, field)


if __name__ == "__main__":
    unittest.main()
