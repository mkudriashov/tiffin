import { $, api, counted, dayName, el, fail, fmtInt, kg, num, plural, slotLabel, state } from "./helpers.js";
import { cook, per100 } from "./cook.js";
import { show } from "./app.js";
import { tracking } from "./more.js";

/* ---------------------------------------------------------------- shopping */
// mirrors telegram.render's RETAILER_NAMES: a code on the row ("marks_spencer") is not a word
// a person reads at a shelf edge ("M&S") - kept in step with the Python names, the way
// counted() above already mirrors menuflow.counted() rather than importing bot code.
export const RETAILER_NAMES = {
  sainsburys: "Sainsbury's", marks_spencer: "M&S", tesco: "Tesco",
  wholefoods: "Whole Foods", ocado: "Ocado",
};

const money = (minor, currency = "GBP") => {
  const symbol = { GBP: "£", EUR: "€", USD: "$" }[currency];
  const digits = (state.units.zero_decimal_currencies || []).includes(currency)
    && minor % 100 === 0 ? 0 : 2;
  return `${symbol || `${currency} `}${(minor / 100).toFixed(digits)}`;
};

// what a tick redraws besides its own row: the count of what is in the trolley
export let onShoppingTick = null;

// Each pack a shop window opens and the days that cook from it - "Chicken thigh fillets
// 1 kg: Tue, Wed, Thu; 120 g left" - so a pack bought on Sunday is seen to be for the week, and
// what will be over is said before it is bought.
export function packsPanel(window, question = null) {
  const box = el("section", { class: "packs", "aria-label": "Packs this shop" });
  box.append(el("h3", { text: `Packs for ${window.span}` }));
  const ul = el("ul", { class: "pack-lines" });
  for (const text of window.lines) ul.append(el("li", { text }));
  box.append(ul);
  // "that's fine, but also ask her"). /packs in the bot changes it later
  if (question) {
    const said = el("p", { class: "muted", role: "status" });
    const row = el("div", { class: "row", role: "group", "aria-label": "Meats or fish a week" });
    for (const [label, mains] of [["Two is fine", 2], ["Three", 3], ["No limit", 0]]) {
      const b = el("button", { type: "button", text: label });
      b.addEventListener("click", async () => {
        for (const x of row.querySelectorAll("button")) x.disabled = true;
        try {
          const got = await api("api/packs/mains", { body: { mains } });
          row.remove();
          said.textContent = got.said;
        } catch (e) {
          for (const x of row.querySelectorAll("button")) x.disabled = false;
          fail({ message: e.message, retry: () => b.click() });
        }
      });
      row.append(b);
    }
    box.append(el("p", { text: question }), row, said);
  }
  return box;
}

export async function renderShopping() {
  // a message links to the list it is about (?list=): on a Saturday the weekend's and Monday's
  // are both live, and the page's own pick would be the wrong one for one of them
  const asked = new URLSearchParams(location.search).get("list");
  const data = await api(asked ? `api/shopping?list=${encodeURIComponent(asked)}` : "api/shopping");
  // what they said is at home already is not to buy: its own section at the end, each a tap from
  const haveItems = (data.items || []).filter((i) => i.have);
  data.items = (data.items || []).filter((i) => !i.have);
  const head = $("#shopping-head");
  const body = $("#shopping-body");
  body.replaceChildren();
  for (const other of data.also || []) {
    const label = other.kind === "joint_weekend"
      ? `The weekend's list (${dayName(other.segment_start)} to ${dayName(other.segment_end)})`
      : `The list for ${dayName(other.segment_start)} to ${dayName(other.segment_end)}`;
    const link = el("a", { href: `?tab=shopping&list=${other.id}`, text: label });
    link.addEventListener("click", (e) => {
      e.preventDefault();
      show("shopping", { list: String(other.id) }).catch((err) => fail(err.message));
    });
    body.append(el("p", { class: "muted" }, "Also: ", link));
  }
  if (!data.list) {
    head.textContent = "";
    body.append(el("p", { class: "muted",
      text: tracking()
        ? "You track only: no menus, so nothing to buy from them. How you plan, in More, changes that."
        : "No shopping list yet. It arrives on its own before each shop once there is a menu; /cook in Telegram builds a menu now." }));
    return;
  }
  // Send this exact list as text, since a page link resolves to the recipient's own list.
  // Sending again includes only items that remain unticked.
  const sendBox = el("p", { class: "muted" });
  const send = el("button", { type: "button", class: "link", text: "Send to Telegram to forward" });
  const sentSaid = el("span", { role: "status" });
  send.addEventListener("click", async () => {
    send.disabled = true;
    try {
      await api("api/shopping/send", { body: { list_id: data.list.id } });
      sentSaid.textContent = " Sent: what is ticked or at home is left off. Forward it from Telegram.";
    } catch (e) {
      fail(e.message);
    } finally {
      send.disabled = false;
    }
  });
  sendBox.append(send, sentSaid);
  body.append(sendBox);
  // an old message's link to a list since rebuilt: this is the one for those days now
  if (data.list.replaced_id) {
    body.append(el("p", { class: "muted",
      text: "The list you opened has since been rebuilt. This is the one for those days now." }));
  }
  if (shoppingSaid) {
    body.append(el("p", { class: "muted", role: "status", text: shoppingSaid }));
    shoppingSaid = null;
  }
  // before the trolley: what each pack is for, read before the shop rather than after it
  if (data.packs && data.packs.lines && data.packs.lines.length) body.append(packsPanel(data.packs));
  const priced = data.items.filter((i) => i.price_pence !== null && i.price_pence !== undefined);
  const pricedCurrencies = new Set(priced.map((i) => i.currency || "GBP"));
  const total = priced.reduce((sum, i) => sum + i.price_pence, 0);
  const cost = pricedCurrencies.size > 1 ? " · prices in different currencies" : priced.length
    ? ` · about ${money(total, priced[0].currency)}${priced.length === data.items.length ? "" : ` (${priced.length} of ${data.items.length} priced)`}`
    : "";
  // "Shopping Mon 14 to Fri 18 Sep" is the bot's own heading for this same list - two lists can
  // sit in one chat (this segment's, and the joint weekend one) and an undated heading here
  // reads the same for both, so shopping from the wrong one is invisible (shopping_tab/07)
  const when = data.list.segment_start
    ? `${dayName(data.list.segment_start)} to ${dayName(data.list.segment_end)} · `
    : "";
  // a weekend list with one person's food on it is not "both of you"
  const whose = data.list.kind === "joint_weekend"
    ? (data.list.for_both === false ? "Weekend · " : "Weekend, both of you · ")
    : "";
  head.textContent = `${when}${whose}${plural(data.items.length, "item")}${cost}`;
  // which day the shopping is for: "I did not shop on Sunday, I shop on Monday and cook
  // nothing is bought for a day that will not be cooked. Past days stay visible, greyed.
  const listDays = data.list.days || [];
  if (data.list.kind === "segment" && listDays.length > 1) {
    body.append(el("p", { class: "muted", text: "Cooking from:" }));
    const picks = el("div", { class: "row list-from" });
    const pick = async (day) => {
      for (const b of picks.querySelectorAll("button")) b.disabled = true;
      try {
        const got = await api("api/shopping/from", { body: { list_id: data.list.id, day } });
        await show("shopping", { list: String(got.list_id) });
      } catch (e) {
        // the days are drawn again before Retry is offered, and Retry sends the same day again
        // rather than clicking a button the redraw has replaced
        await renderShopping().catch((err) => fail(err.message));
        fail({ message: e.message, retry: () => pick(day) });
      }
    };
    for (const day of listDays) {
      const btn = el("button", { type: "button", text: dayName(day),
        class: day === data.list.from_day ? "on" : "" });
      btn.disabled = day < state.today;
      btn.setAttribute("aria-pressed", day === data.list.from_day ? "true" : "false");
      btn.addEventListener("click", () => {
        if (day !== data.list.from_day) pick(day);
      });
      picks.append(btn);
    }
    body.append(picks);
  }
  // ... I skip this meal and it already removes it from the shopping list"): each meal with what
  // it needs, and Skip on one takes its food off the list. The choice is kept on this phone.
  const meals = data.meals || [];
  let view = "food";
  try {
    view = localStorage.getItem("tiffin.shopping-view") === "meal" ? "meal" : "food";
  } catch (e) { /* private mode: by food every time */ }
  if (meals.length) {
    const views = el("div", { class: "row", role: "group", "aria-label": "Show the list" });
    for (const [key, word] of [["food", "By food"], ["meal", "By meal"]]) {
      const b = el("button", { type: "button", text: word, "aria-pressed": String(view === key) });
      if (view === key) b.classList.add("on");
      b.addEventListener("click", () => {
        if (view === key) return;
        try {
          localStorage.setItem("tiffin.shopping-view", key);
        } catch (e) { /* not kept: the next visit opens by food */ }
        renderShopping().catch((err) => fail(err.message));
      });
      views.append(b);
    }
    body.append(views);
  }
  if (meals.length && view === "meal") {
    shoppingByMeal(body, meals);
    return;
  }
  // how far through the shop: the count and what is left to pay, kept up to date by every tick.
  // Ticked lines sit together at the end the next time the list is drawn, never under the
  // thumb that just ticked one
  const progress = el("div", { class: "trolley", role: "status", "aria-live": "polite" });
  const bar = el("div", { class: "bar" }, el("i", {}));
  const said = el("p", { class: "muted" });
  progress.append(said, bar);
  const drawProgress = () => {
    const done = data.items.filter((i) => i.ticked);
    const left = data.items.filter((i) => !i.ticked && i.price_pence !== null && i.price_pence !== undefined)
      .reduce((sum, i) => sum + i.price_pence, 0);
    // what is left to pay only once the shop has started: before the first tick it is the total
    // the heading already says
    said.textContent = `${done.length} of ${data.items.length} in the trolley`
      + (priced.length && pricedCurrencies.size === 1 && done.length && done.length < data.items.length ? ` · about ${money(left, priced[0].currency)} still to buy` : "");
    bar.firstChild.style.width = `${data.items.length ? (done.length / data.items.length) * 100 : 0}%`;
  };
  onShoppingTick = data.items.length ? drawProgress : null;
  if (data.items.length) {
    drawProgress();
    body.append(progress);
  }
  // say the shop only when the list actually draws on more than one - mirrors shopping.py's
  // own `say_shop` rule: a list that only ever reads Sainsbury's does not need told so
  const retailers = new Set(data.items.map((i) => i.retailer).filter(Boolean));
  const toBuy = data.items.filter((i) => !i.ticked);
  // what keeps for months is usually at home already: checked first, before the shop, so the
  // list that leaves the house is only what is really missing. Not ticked
  // for being at home: a tick is a fact about the trolley, and a list rebuilt for the same days
  // credits every ticked line as whole packs bought - 500 g of rice in the cupboard became a
  // 1 kg bag, carried forward, and the next week bought none
  const first = toBuy.filter((i) => i.long_life);
  const rest = toBuy.filter((i) => !i.long_life);
  // each aisle a block of its own: two columns of them on a wide screen, one on a phone
  const aisles = el("div", { class: "shop-aisles" });
  body.append(aisles);
  if (first.length && rest.length) {
    const group = el("section", { class: "aisle" });
    group.append(el("h2", { text: "Check at home first" }));
    group.append(el("p", { class: "muted", text: "These keep for months: look in the cupboard before you go. A tick means it is in the trolley." }));
    const ul = el("ul", { class: "items shop" });
    for (const item of first) ul.append(shoppingRow(item, retailers, true));
    group.append(ul);
    aisles.append(group);
  }
  let aisle = null;
  let list = null;
  for (const item of data.items) {
    if (item.ticked) continue;  // in the trolley: drawn together at the end
    if (first.length && rest.length && item.long_life) continue;  // drawn above, checked first
    if (item.aisle !== aisle) {
      aisle = item.aisle;
      const group = el("section", { class: "aisle" });
      group.append(el("h2", { text: aisleName(aisle) }));
      list = el("ul", { class: "items shop" });
      group.append(list);
      aisles.append(group);
    }
    list.append(shoppingRow(item, retailers));
  }
  // in the trolley already: together at the end, folded, each still a tap from coming back
  const ticked = data.items.filter((i) => i.ticked);
  if (ticked.length) {
    const box = el("details", { class: "fold trolley-done" });
    box.append(el("summary", { text: `In the trolley (${ticked.length})` }));
    const ul = el("ul", { class: "items shop" });
    for (const item of ticked) ul.append(shoppingRow(item, retailers, Boolean(item.long_life)));
    box.append(ul);
    body.append(box);
  }

  if (haveItems.length) {
    const box = el("section", { class: "aisle have" });
    box.append(el("h2", { text: `At home, you said (${haveItems.length})` }));
    const ul = el("ul", { class: "items shop" });
    for (const item of haveItems) {
      const back = el("button", { type: "button", class: "link", text: "Buy it after all" });
      back.addEventListener("click", () => setHave(item, false, back));
      ul.append(el("li", { class: "have-line" },
        el("span", { class: "name", text: item.name }),
        el("span", { class: "muted", text: ` · ${item.packs} x ${item.pack_label}` }), " ", back));
    }
    box.append(ul);
    body.append(box);
  }
  // what the list counts on being at home already, and so buys none of: said, never dropped
  const home = data.at_home || [];
  if (home.length) {
    body.append(el("h2", { text: "Should be at home already" }));
    body.append(el("p", { class: "muted",
      text: "Counted as left over from an earlier shop, so this list buys none of it. Worth a look before you go." }));
    const ul = el("ul", { class: "items at-home" });
    for (const food of home) {
      ul.append(el("li", {},
        el("span", { text: food.name }),
        el("span", { class: "muted", text: `${num(food.grams)} g for these days` })));
    }
    body.append(ul);
  }
  if (!data.items.length && !home.length) {
    body.append(el("p", { class: "muted", text: "Nothing to buy for these days." }));
  }
}

// "produce" is the catalogue's word; a heading is a heading
export const aisleName = (aisle) => {
  const word = String(aisle || "other").replace(/_/g, " ");
  return word.charAt(0).toUpperCase() + word.substring(1);
};

// The list by meal: each day's meals with what each needs at her size, and Skip (or Have it back)
// on each. A skip is the person's own kind (`/set skip`): hers drops the meal and leaves the rest
// of the day as planned. The list is built again from the day's plates the next time it is read.
export function shoppingByMeal(body, meals) {
  body.append(el("p", { class: "muted",
    text: "Each meal with what it needs. Skip a meal and its food comes off the list." }));
  let day = null;
  let ul = null;
  for (const meal of meals) {
    if (meal.day !== day) {
      day = meal.day;
      body.append(el("h2", { text: dayName(day) }));
      ul = el("ul", { class: "items meals" });
      body.append(ul);
    }
    const li = el("li", { class: meal.skipped ? "skipped" : "" });
    li.append(el("div", {},
      el("strong", { text: `${slotLabel(meal.slot)}: ` }),
      el("span", { text: meal.name }),
      el("span", { class: "muted",
        text: meal.skipped ? " · skipped, nothing bought for it" : ` · ${fmtInt(meal.kcal)} kcal` })));
    li.append(el("p", { class: "muted",
      text: meal.ingredients.map((i) => `${i.name} ${num(i.grams)} g`).join(", ") }));
    if (meal.can_change) {
      const word = meal.skipped ? "Have it back" : "Skip";
      const b = el("button", { type: "button", text: word,
        "aria-label": `${word}: ${slotLabel(meal.slot)} on ${dayName(meal.day)}` });
      b.addEventListener("click", async () => {
        b.disabled = true;
        let got;
        try {
          got = await api("api/shopping/skip", { body: { day: meal.day, slot: meal.slot, skip: !meal.skipped } });
        } catch (e) {
          b.disabled = false;
          if (e.status >= 400 && e.status < 500) {
            li.append(el("p", { class: "plate-said", role: "status", text: e.message }));
          } else {
            fail({ message: e.message, retry: () => b.click() });
          }
          return;
        }
        await renderShopping();
        $("#shopping-body").prepend(el("p", { class: "plate-said", role: "status", text: got.said }));
      });
      li.append(b);
    }
    ul.append(li);
  }
}

// a small downward chevron, drawn: the glyph it replaced was missing from a phone's font
export function chevron() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "chev");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M6 9l6 6 6-6");
  svg.append(path);
  return svg;
}

// One row of the shopping list: a box to tick (in the trolley), the food, how many packs and
// what they cost. The rest - the pack's name, what it is for, its per-100 g, the way to say the
// shop's shelf was empty - opens under it on a tap: in the shop those made every line five lines
// tall and the list twenty screens long.
// what the last "I have it" or "Buy it after all" did, said at the top of the list it redrew
export let shoppingSaid = null;

// "I have it": the line comes off this list for its days, said back, and comes back with one tap
export async function setHave(item, have, button) {
  if (button) button.disabled = true;
  try {
    const got = await api("api/shopping/have", { body: { item_id: item.id, have } });
    shoppingSaid = got.said;
    await renderShopping();
  } catch (e) {
    if (button) button.disabled = false;
    fail({ message: e.message, retry: () => setHave(item, have, button) });
  }
}

export function shoppingRow(item, retailers, atHome = false) {
  const li = el("li", { class: item.ticked ? "done" : "" });
  const shop = retailers.size > 1 ? RETAILER_NAMES[item.retailer] || item.retailer || "" : "";
  // standing in Sainsbury's holding a line for M&S is the whole failure this tag exists to
  // stop - unless the shop is already named in the product itself ("Tesco Broccoli 375G"). It
  // has a line of its own under the name: squeezed into a column beside a long product name it
  // painted one letter wide
  const tag = shop && !(item.product_name || "").toLowerCase().includes(shop.toLowerCase())
    ? `[${shop}]` : "";
  // what the list is counting on already being at home, so buying fewer packs is not a
  // silent guess - the bot's own line says "(350 g in)" and the page said nothing at all
  const credit = item.credit_g > 0 ? `(${num(item.credit_g)} g in)` : "";
  const whose = item.whose ? item.whose : "";
  const sub = [tag, credit, whose, item.out ? "shelf empty, marked" : ""].filter(Boolean).join(" · ");
  const price = item.price_pence !== null && item.price_pence !== undefined
    ? money(item.price_pence, item.currency) : "";
  const button = el("button", { type: "button", class: "tick" },
    el("span", { class: "box", "aria-hidden": "true" }),
    el("span", { class: "what" },
      el("span", { class: "name", text: item.name }),
      sub ? el("span", { class: "muted sub", text: sub }) : null),
    el("span", { class: "qty" },
      el("span", { text: `${item.packs} x ${item.pack_label}` }),
      price ? el("span", { class: "muted", text: price }) : null));
  // ticked is said, not only struck through, so a screen reader hears which are in the trolley
  button.setAttribute("aria-pressed", item.ticked ? "true" : "false");
  button.addEventListener("click", async () => {
    const ticked = !li.classList.contains("done");
    li.classList.toggle("done", ticked);
    button.setAttribute("aria-pressed", ticked ? "true" : "false");
    item.ticked = ticked;
    if (onShoppingTick) onShoppingTick();
    try {
      await api("api/shopping/tick", { body: { item_id: item.id, ticked } });
    } catch (e) {
      li.classList.toggle("done", !ticked);
      button.setAttribute("aria-pressed", ticked ? "false" : "true");
      item.ticked = !ticked;
      if (onShoppingTick) onShoppingTick();
      // "that tick did not save" needs a way back, not a toast that deletes itself while the
      // item sits un-ticked again - tapping Retry replays the exact same click
      fail({ message: e.message, retry: () => button.click() });
    }
  });
  // everything else about the line, a tap away on its own small button
  const more = el("button", { type: "button", class: "more", "aria-expanded": "false",
    "aria-label": `More about ${item.name}` },
  chevron());
  const detail = el("div", { class: "item-detail", hidden: true });
  more.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    more.setAttribute("aria-expanded", String(!detail.hidden));
  });
  if (item.product_name) detail.append(el("p", { text: item.product_name }));
  if (item.product_url) {
    let productUrl;
    try {
      const parsed = new URL(item.product_url);
      if (parsed.protocol === "https:") productUrl = parsed.href;
    } catch (_) { /* malformed links are omitted */ }
    if (productUrl) detail.append(el("p", {}, el("a", {
      href: productUrl, text: "Product link", target: "_blank", rel: "noopener noreferrer",
    })));
  }
  const productForm = el("form", { class: "food-form" });
  const productName = el("input", { type: "text", required: "", maxlength: "200",
    value: item.product_name || item.name });
  const productPack = el("input", { type: "number", required: "", min: "0.1", max: "20000",
    step: "any", value: item.pack_g || "" });
  const productPrice = el("input", { type: "number", min: "0",
    step: state.units.price_decimals === 0 ? "1" : "0.01" });
  const productShop = el("input", { type: "text", maxlength: "80", value: item.retailer || "" });
  const productUrl = el("input", { type: "url", placeholder: "https://…" });
  const productSave = el("button", { type: "submit", text: "Save my product" });
  const productSaid = el("span", { role: "status", class: "muted" });
  productForm.append(
    el("label", {}, "Product name", productName),
    el("label", {}, "Pack size (g)", productPack),
    el("label", {}, `Price in ${state.units.currency || "GBP"} (optional)`, productPrice),
    el("label", {}, "Shop (optional)", productShop),
    el("label", {}, "Product link (optional)", productUrl),
    productSave, productSaid,
  );
  productForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    productSave.disabled = true;
    try {
      const result = await api("api/shopping/product", { body: {
        item_id: item.id, name: productName.value, pack_g: productPack.value,
        price: productPrice.value, shop: productShop.value, url: productUrl.value,
      } });
      productSaid.textContent = result.said;
    } catch (error) {
      fail(error.message);
    } finally {
      productSave.disabled = false;
    }
  });
  const productEditor = el("details", {}, el("summary", { text: "Use my product" }), productForm);
  detail.append(productEditor);
  // what it is for, and how often it is bought: said under the name, never instead of it
  const forLine = (item.for_dishes || []).length ? `for ${item.for_dishes.join(", ")}` : "";
  const regular = item.regular ? (forLine ? ` · ${item.regular}` : item.regular) : "";
  if (forLine || regular) detail.append(el("p", { class: "muted for", text: forLine + regular }));
  // in the shop, holding a tub: is this the same food the menu was built from? The
  // catalogue's own per 100 g is the only thing that answers it, and the row never said
  const what = per100(item);
  if (what) detail.append(el("p", { class: "muted", text: what }));
  // the one dead end on the row: tick meant bought, and there was no way to say the shelf
  // was empty - either the row sits untouched and the menu keeps planning around a tin
  // nobody can buy, or it gets ticked and Tiffin believes it is in the fridge
  const outRow = el("div", { class: "out-row" });
  // under "Check at home first" the same tap names the shop: "Shelf empty?" there read as "none
  // in my cupboard", and recorded that the shop had none
  const askedOut = atHome ? "Not in the shop?" : "Shelf empty?";
  const outBtn = el("button", { type: "button", class: "link out", text: askedOut });
  const drawMarked = () => {
    const back = el("button", { type: "button", class: "link out", text: "Undo" });
    back.addEventListener("click", (e) => { e.stopPropagation(); markOut(true); });
    outRow.replaceChildren(el("span", { class: "muted", text: "Marked: not asked again for a while. " }), back);
  };
  const markOut = async (undo) => {
    outBtn.disabled = true;
    try {
      await api("api/shopping/out", { body: { item_id: item.id, undo } });
      if (undo) {
        outRow.replaceChildren(outBtn);
        outBtn.textContent = askedOut;
      } else drawMarked();
    } catch (err) {
      fail({ message: err.message, retry: () => markOut(undo) });
    } finally {
      outBtn.disabled = false;
    }
  };
  outBtn.addEventListener("click", (e) => { e.stopPropagation(); markOut(false); });
  // marked already by this person on the page: drawn as marked, with its Undo, after any redraw.
  // Never the other one's mark - their Undo is theirs to tap (queries._shelf_marked)
  if (item.out) drawMarked();
  else outRow.append(outBtn);
  detail.append(outRow);
  // what Sainsbury's said about a line that does not name one of its products: found after
  // folded, because it changes what to pick up
  const shopNote = item.shop_note
    ? el("p", { class: `shop-note ${item.shop_status || ""}`, text: item.shop_note })
    : null;
  // a pack an earlier day opened should cover this line: said in sight, with the one tap that
  // takes it off - and "I have it" for any line, folded under it
  const haveBtn = el("button", { type: "button", class: "link have", text: "I have it" });
  haveBtn.addEventListener("click", (e) => { e.stopPropagation(); setHave(item, true, haveBtn); });
  const cover = item.covered && !item.ticked
    ? el("p", { class: "cover-note" }, el("span", { class: "muted", text: `${item.covered}. ` }), haveBtn)
    : null;
  if (!cover && item.have !== undefined) {
    const own = el("button", { type: "button", class: "link have", text: "I have it already" });
    own.addEventListener("click", (e) => { e.stopPropagation(); setHave(item, true, own); });
    detail.append(el("div", { class: "out-row" }, own));
  }
  li.append(el("div", { class: "line" }, button, more), ...(shopNote ? [shopNote] : []), ...(cover ? [cover] : []), detail);
  return li;
}

