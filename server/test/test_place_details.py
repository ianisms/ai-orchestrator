"""Tests for place details formatter and helpers (tools_server/tools/location/places.py).

Covers:
- _format_place_details: full data, partial data, empty data
- _extract_summary_text: string, nested dict, dict with text.text
- _PRICE_LABELS mapping
- Hours formatting with open/closed status
- Services, food flags, atmosphere flags
- Reviews formatting with author, rating, time
- Editorial vs generative summary priority
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

# Load places.py directly to avoid the __init__.py import chain
# which pulls in prometheus_client and other runtime-only deps.
_PLACES_PATH = Path("/ai/server/tools_server/tools/location/places.py")
_UTILS_DIR = Path("/ai/server/tools_server")
if str(_UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(_UTILS_DIR))

_spec = importlib.util.spec_from_file_location("_places", _PLACES_PATH)
_places = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_places)

_extract_summary_text = _places._extract_summary_text
_format_place_details = _places._format_place_details
_PRICE_LABELS = _places._PRICE_LABELS


class TestExtractSummaryText(unittest.TestCase):
    """Verify _extract_summary_text handles all input shapes."""

    def test_plain_string(self):
        self.assertEqual(_extract_summary_text("  hello  "), "hello")

    def test_empty_string(self):
        self.assertEqual(_extract_summary_text(""), "")

    def test_dict_with_text_key(self):
        self.assertEqual(_extract_summary_text({"text": "summary here"}), "summary here")

    def test_dict_with_overview_key(self):
        self.assertEqual(_extract_summary_text({"overview": "my overview"}), "my overview")

    def test_nested_text_text(self):
        self.assertEqual(
            _extract_summary_text({"text": {"text": "deep text"}}),
            "deep text",
        )

    def test_none_returns_empty(self):
        self.assertEqual(_extract_summary_text(None), "")

    def test_dict_no_matching_keys(self):
        self.assertEqual(_extract_summary_text({"foo": "bar"}), "")


class TestFormatPlaceDetailsBasic(unittest.TestCase):
    """Verify _format_place_details with minimal and full data."""

    def test_empty_data(self):
        result = _format_place_details({})
        self.assertEqual(result, "No details available for that place.")

    def test_name_only(self):
        data = {
            "displayName": {"text": "Joe's Pizza"},
            "formattedAddress": "123 Main St",
        }
        result = _format_place_details(data)
        self.assertIn("Joe's Pizza", result)
        self.assertIn("123 Main St", result)

    def test_name_with_primary_type(self):
        data = {
            "displayName": {"text": "Sakura"},
            "primaryTypeDisplayName": {"text": "Japanese restaurant"},
            "formattedAddress": "456 Oak Ave",
        }
        result = _format_place_details(data)
        self.assertIn("Sakura (Japanese restaurant)", result)

    def test_rating_and_count(self):
        data = {
            "displayName": {"text": "Test Place"},
            "rating": 4.5,
            "userRatingCount": 200,
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("4.5 stars", result)
        self.assertIn("200 reviews", result)

    def test_rating_without_count(self):
        data = {
            "displayName": {"text": "Test"},
            "rating": 3.0,
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("3.0 stars", result)
        self.assertNotIn("reviews", result)


class TestFormatPlaceDetailsPrice(unittest.TestCase):
    """Verify price level and price range formatting."""

    def test_price_level_enum(self):
        for level, label in _PRICE_LABELS.items():
            data = {
                "displayName": {"text": "P"},
                "priceLevel": level,
                "formattedAddress": "x",
            }
            result = _format_place_details(data)
            self.assertIn(f"Price: {label}", result)

    def test_price_range(self):
        data = {
            "displayName": {"text": "P"},
            "priceRange": {
                "startPrice": {"units": "10", "currencyCode": "USD"},
                "endPrice": {"units": "30", "currencyCode": "USD"},
            },
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Price range: 10 to 30", result)


class TestFormatPlaceDetailsHours(unittest.TestCase):
    """Verify hours and open/closed status formatting."""

    def test_weekday_descriptions(self):
        data = {
            "displayName": {"text": "H"},
            "regularOpeningHours": {
                "weekdayDescriptions": [
                    "Monday: 9:00 AM - 5:00 PM",
                    "Tuesday: 9:00 AM - 5:00 PM",
                ],
                "openNow": True,
            },
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Hours:", result)
        self.assertIn("Monday: 9:00 AM - 5:00 PM", result)
        self.assertIn("Currently: Open", result)

    def test_closed_status(self):
        data = {
            "displayName": {"text": "H"},
            "regularOpeningHours": {
                "weekdayDescriptions": ["Monday: Closed"],
                "openNow": False,
            },
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Currently: Closed", result)

    def test_current_opening_hours_overrides(self):
        data = {
            "displayName": {"text": "H"},
            "regularOpeningHours": {
                "weekdayDescriptions": ["Mon: 9-5"],
                "openNow": False,
            },
            "currentOpeningHours": {"openNow": True},
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Currently: Open", result)


class TestFormatPlaceDetailsServices(unittest.TestCase):
    """Verify service flags formatting."""

    def test_all_services(self):
        data = {
            "displayName": {"text": "S"},
            "dineIn": True,
            "takeout": True,
            "delivery": True,
            "curbsidePickup": True,
            "reservable": True,
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("dine-in", result)
        self.assertIn("takeout", result)
        self.assertIn("delivery", result)
        self.assertIn("curbside pickup", result)
        self.assertIn("reservations accepted", result)

    def test_no_services(self):
        data = {
            "displayName": {"text": "S"},
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertNotIn("Services:", result)


class TestFormatPlaceDetailsFoodFlags(unittest.TestCase):
    """Verify food/cuisine flag formatting."""

    def test_food_flags(self):
        data = {
            "displayName": {"text": "F"},
            "servesBreakfast": True,
            "servesDinner": True,
            "servesWine": True,
            "servesVegetarianFood": True,
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("breakfast", result)
        self.assertIn("dinner", result)
        self.assertIn("wine", result)
        self.assertIn("vegetarian options", result)

    def test_no_food_flags(self):
        data = {"displayName": {"text": "F"}, "formattedAddress": "x"}
        result = _format_place_details(data)
        self.assertNotIn("Serves:", result)


class TestFormatPlaceDetailsAtmosphere(unittest.TestCase):
    """Verify atmosphere/feature flags formatting."""

    def test_all_atmosphere(self):
        data = {
            "displayName": {"text": "A"},
            "goodForGroups": True,
            "goodForChildren": True,
            "outdoorSeating": True,
            "liveMusic": True,
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("good for groups", result)
        self.assertIn("outdoor seating", result)
        self.assertIn("live music", result)


class TestFormatPlaceDetailsSummaries(unittest.TestCase):
    """Verify editorial/generative summary and review summary."""

    def test_editorial_preferred_over_generative(self):
        data = {
            "displayName": {"text": "X"},
            "editorialSummary": {"text": "Editorial desc"},
            "generativeSummary": {"text": "AI desc"},
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Description: Editorial desc", result)

    def test_generative_fallback(self):
        data = {
            "displayName": {"text": "X"},
            "generativeSummary": {"overview": {"text": "AI overview"}},
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Description: AI overview", result)

    def test_review_summary(self):
        data = {
            "displayName": {"text": "X"},
            "reviewSummary": {"text": "Great food overall"},
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Review summary: Great food overall", result)


class TestFormatPlaceDetailsReviews(unittest.TestCase):
    """Verify individual reviews formatting."""

    def test_review_with_all_fields(self):
        data = {
            "displayName": {"text": "R"},
            "reviews": [
                {
                    "rating": 5,
                    "relativePublishTimeDescription": "2 weeks ago",
                    "authorAttribution": {"displayName": "Alice"},
                    "text": {"text": "Amazing food!"},
                }
            ],
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Reviews:", result)
        self.assertIn("5 stars", result)
        self.assertIn("Alice", result)
        self.assertIn("2 weeks ago", result)
        self.assertIn("Amazing food!", result)

    def test_review_originalText_preferred(self):
        data = {
            "displayName": {"text": "R"},
            "reviews": [
                {
                    "rating": 4,
                    "originalText": {"text": "Original review"},
                    "text": {"text": "Translated review"},
                }
            ],
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Original review", result)

    def test_review_without_text_skipped(self):
        data = {
            "displayName": {"text": "R"},
            "reviews": [
                {"rating": 3},
            ],
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertNotIn("3 stars", result)

    def test_phone_and_website(self):
        data = {
            "displayName": {"text": "C"},
            "nationalPhoneNumber": "(555) 123-4567",
            "websiteUri": "https://example.com",
            "formattedAddress": "x",
        }
        result = _format_place_details(data)
        self.assertIn("Phone: (555) 123-4567", result)
        self.assertIn("Website: https://example.com", result)


class TestFormatPlaceDetailsFull(unittest.TestCase):
    """Integration test: full realistic API response."""

    def test_full_response(self):
        data = {
            "displayName": {"text": "Sakura Sushi"},
            "primaryTypeDisplayName": {"text": "Japanese restaurant"},
            "formattedAddress": "123 Oak Ave, Springfield, IL 62701",
            "rating": 4.7,
            "userRatingCount": 532,
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "nationalPhoneNumber": "(732) 555-0199",
            "websiteUri": "https://sakurasushi.example.com",
            "regularOpeningHours": {
                "weekdayDescriptions": [
                    "Monday: 11:30 AM - 9:30 PM",
                    "Tuesday: 11:30 AM - 9:30 PM",
                ],
                "openNow": True,
            },
            "dineIn": True,
            "takeout": True,
            "delivery": True,
            "reservable": True,
            "servesDinner": True,
            "servesBeer": True,
            "servesWine": True,
            "goodForGroups": True,
            "outdoorSeating": True,
            "editorialSummary": {"text": "Upscale Japanese dining with fresh sushi."},
            "reviewSummary": {"text": "Consistently praised for quality fish."},
            "reviews": [
                {
                    "rating": 5,
                    "relativePublishTimeDescription": "a month ago",
                    "authorAttribution": {"displayName": "John D."},
                    "text": {"text": "Best sushi in the county!"},
                },
            ],
        }
        result = _format_place_details(data)

        self.assertIn("Sakura Sushi (Japanese restaurant)", result)
        self.assertIn("123 Oak Ave", result)
        self.assertIn("4.7 stars", result)
        self.assertIn("532 reviews", result)
        self.assertIn("Price: Moderate", result)
        self.assertIn("(732) 555-0199", result)
        self.assertIn("Hours:", result)
        self.assertIn("Currently: Open", result)
        self.assertIn("dine-in", result)
        self.assertIn("reservations accepted", result)
        self.assertIn("dinner", result)
        self.assertIn("wine", result)
        self.assertIn("good for groups", result)
        self.assertIn("outdoor seating", result)
        self.assertIn("Upscale Japanese dining", result)
        self.assertIn("Consistently praised", result)
        self.assertIn("Best sushi in the county!", result)


if __name__ == "__main__":
    unittest.main()
