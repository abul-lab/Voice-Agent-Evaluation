"""Product catalogue: loading and read-only queries. The catalogue is the only source of product facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from voiceshop.errors import ConfigError


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    category: str
    price: int
    weight_kg: float
    capacity: int
    season: int  # 2 = summer, 3 = three-season, 4 = winter
    stock: int

    @property
    def first_word(self) -> str:
        return self.name.split()[0].lower()


class Catalog:
    def __init__(self, products: list[Product], categories: dict[str, list[str]], lightweight_max_kg: float,
                 recommend_max: int, store_name: str) -> None:
        self.products = {p.id: p for p in products}
        self.categories = categories
        self.lightweight_max_kg = lightweight_max_kg
        self.recommend_max = recommend_max
        self.store_name = store_name
        self.prices = {p.price for p in products}

    @classmethod
    def load(cls, path: Path) -> Catalog:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"catalogue not readable: {path}") from exc
        products = [Product(**p) for p in doc["products"]]
        ids = [p.id for p in products]
        if len(ids) != len(set(ids)):
            raise ConfigError("duplicate product ids in catalogue")
        for p in products:
            if p.category not in doc["categories"]:
                raise ConfigError(f"product {p.id} has unknown category {p.category}")
        return cls(products, doc["categories"], doc["rules"]["lightweight_max_kg"],
                   doc["rules"]["recommend_max"], doc["store"]["name"])

    def get(self, product_id: str) -> Product:
        return self.products[product_id]

    def in_category(self, category: str) -> list[Product]:
        return [p for p in self.products.values() if p.category == category]

    def is_lightweight(self, product: Product) -> bool:
        return product.weight_kg <= self.lightweight_max_kg
