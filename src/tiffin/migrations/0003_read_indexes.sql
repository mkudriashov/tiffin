CREATE INDEX IF NOT EXISTS ix_recipe_ingredients_recipe ON recipe_ingredients(recipe_id);
CREATE INDEX IF NOT EXISTS ix_menu_days_menu_day ON menu_days(menu_id, day);
CREATE INDEX IF NOT EXISTS ix_meal_slots_day_slot ON meal_slots(menu_day_id, slot);
CREATE INDEX IF NOT EXISTS ix_ratings_person_recipe ON ratings(person_id, recipe_id, id DESC);
