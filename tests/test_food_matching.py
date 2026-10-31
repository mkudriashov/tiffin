"""Name normalisation used by catalogue and food preferences."""

from tiffin.food.text import jaccard, normalise, tokens


def test_text_helpers() -> None:
    assert normalise("  Chicken Breast, (Raw) - 100g ") == "chicken breast raw 100g"
    assert tokens("Tesco - Chicken Breast Fillets, 100 g") == {
        "tesco",
        "chicken",
        "breast",
        "fillets",
    }
    assert tokens("Fage Total 0% Greek Yogurt 170g") == {"fage", "total", "greek", "yogurt"}
    assert jaccard(frozenset({"a", "b", "c"}), frozenset({"a", "b", "d"})) == 0.5
    assert jaccard(frozenset(), frozenset({"a"})) == 0.0
