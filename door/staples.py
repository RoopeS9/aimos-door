"""Fixed real-catalog candidates for the restaurant's synthetic usual basket.

The IDs keep the demo deterministic. Quotes and availability still come from the
Valio mock API at seed time; the order history itself is explicitly simulated.
"""

RESTAURANT_SEED_VERSION = 2

RESTAURANT_STAPLES = (
    {"key": "cream", "id": "464194", "name": "Valio whipping cream 1 l UHT lactose free", "category": "10401", "product_type": "other", "unit": "L", "pack_size": 1.0, "packs": 2},
    {"key": "butter", "id": "462421", "name": "Valio Eila® butter 200 g lactose free", "category": "10500", "product_type": "butter", "unit": "KG", "pack_size": 0.2, "packs": 2},
    {"key": "cheese", "id": "463400", "name": "Valio Hyvä suomalainen Arki® cheese slices e500 g", "category": "10602", "product_type": "other", "unit": "KG", "pack_size": 0.5, "packs": 2},
    {"key": "eggs", "id": "467880", "name": "Kieku Barn egg L15 1020g", "category": "10590", "product_type": "other", "unit": "PACK", "pack_size": 1.02, "packs": 2},
    {"key": "flour", "id": "460791", "name": "Myllyn Paras Wheat flour 2 kg", "category": "21300", "product_type": "flour", "unit": "KG", "pack_size": 2.0, "packs": 2},
    {"key": "sugar", "id": "460935", "name": "Dansukker Soft Brown Sugar 500g", "category": "22100", "product_type": "other", "unit": "KG", "pack_size": 0.5, "packs": 2},
    {"key": "rice", "id": "463511", "name": "AIMO 10KG PARBOILED LONG GRAIN RICE", "category": "21340", "product_type": "rice", "unit": "KG", "pack_size": 10.0, "packs": 1},
    {"key": "pasta", "id": "460293", "name": "URTEKRAM organic Spaghetti Gluten free 250g", "category": "21380", "product_type": "pasta", "unit": "KG", "pack_size": 0.25, "packs": 5},
    {"key": "tomatoes", "id": "466203", "name": "BRAVO CHEF 10KG CRUSHED TOMATOES BIB", "category": "21590", "product_type": "other", "unit": "KG", "pack_size": 10.0, "packs": 1},
    {"key": "olive oil", "id": "463403", "name": "Memmas Knossos Extra Virgin Olive oil 500 ml", "category": "10531", "product_type": "other", "unit": "L", "pack_size": 0.5, "packs": 1},
    {"key": "chicken", "id": "462209", "name": "Happy Chef 2,5kg (110-130g) Chicken breast roast", "category": "41710", "product_type": "other", "unit": "KG", "pack_size": 2.5, "packs": 2},
    {"key": "coffee", "id": "462090", "name": "Paulig Juhla Mokka filter ground coffee 500g", "category": "22300", "product_type": "other", "unit": "KG", "pack_size": 0.5, "packs": 2},
)
