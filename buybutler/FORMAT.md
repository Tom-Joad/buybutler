# Buybutler's readable copy, format version 1

Buybutler keeps its data in a database (`buybutler.sqlite3`). So that the
lists, products, ratings and prices can be read without Buybutler, it also
writes them here as text files, on its own, shortly after every change.

**This folder is a copy for reading.** Buybutler never reads it back.
Changing a file here changes nothing in Buybutler, and the file is
overwritten the next time its product changes. The whole folder can be
deleted; `buybutler export` writes it again.

## Layout

```
export/
  FORMAT.md                       this file
  1-home/                         one folder per list: its number and name
    list.md                       the list and its members
    products/
      412-natural-yoghurt/        one folder per product: its number and name
        product.md                the product, its ratings and its prices
images/
  products/                       product pictures
  ratings/                        photos attached to ratings
trash/                            folders of deleted products and lists
```

Every text file is UTF-8 with Unix line ends. A folder is renamed when its
list or product gets another name; the number in front stays.

Each file is a YAML header between two `---` lines and nothing else. Texts
are always in double quotes (a barcode keeps its leading zero, a date stays
a text), numbers and `true`/`false` are not. Fields without a value are left
out.

## product.md

```markdown
---
# Written by Buybutler: a copy for reading. Changes here are overwritten, see FORMAT.md.
format: "buybutler/1"
id: 412
list: "Home"
name: "Natural Yoghurt"
brand: "Example Dairy"
barcode: "4000000000001"
category: "Yoghurts"
quantity: "500 g"
source: "openfoodfacts"
photo: "../../../../images/products/Zk3vT0aXw1mQeLr9uYb2Pg.jpg"
created: "2026-03-02T17:41:09.123Z"
updated: "2026-11-14T09:12:40.512Z"
created_by: "tom"
ratings:
  - {id: 88, date: "2026-03-02", by: "tom", buy_again: true, stars: 4, store: "Corner Shop", reason: "creamy", created: "2026-03-02T17:42:00.000Z", updated: "2026-03-02T17:42:00.000Z"}
prices:
  - {id: 301, date: "2026-03-02", by: "tom", amount: 1.99, quantity: 500, unit: "g", store: "Corner Shop", rating: 88, created: "2026-03-02T17:42:00.000Z", updated: "2026-03-02T17:42:00.000Z"}
---
```

| Field | Meaning |
| --- | --- |
| `format` | Always `buybutler/1` for this version of the format |
| `id` | The product's number in Buybutler; it never changes |
| `list` | Name of the list the product belongs to |
| `name`, `brand`, `category`, `quantity` | As entered or as Open Food Facts gave them; `quantity` is the pack size as text |
| `barcode` | The barcode, when the product has one |
| `source` | `manual` (typed in) or `openfoodfacts` (looked up by barcode) |
| `photo` | Path to the product picture, from this file's folder |
| `created`, `updated` | Time stamps in UTC, ISO 8601 |
| `created_by` | User name of who added the product; missing when that user was deleted and the name was not kept |

### ratings

Oldest first; the newest is the verdict that counts.

| Field | Meaning |
| --- | --- |
| `id` | The rating's number |
| `date` | The day of the rating |
| `by` | User name; missing when that user was deleted and the name was not kept |
| `buy_again` | `true`: buy again, `false`: don't |
| `stars` | 1 to 5, optional |
| `store`, `reason` | Optional texts |
| `photo` | Path to the photo attached to the rating |
| `created`, `updated` | Time stamps in UTC |

### prices

Oldest first.

| Field | Meaning |
| --- | --- |
| `id` | The price's number |
| `date` | The day of the purchase |
| `by` | User name; missing when that user was deleted and the name was not kept |
| `amount` | What was paid, in the currency named in the list's `list.md` |
| `quantity`, `unit` | What the amount was for (`500`, `g`), optional |
| `offer` | `true` when it was an offer price; left out otherwise |
| `store` | Optional |
| `rating` | The `id` of the rating the price was entered with, if any |
| `created`, `updated` | Time stamps in UTC |

## list.md

```markdown
---
# Written by Buybutler: a copy for reading. Changes here are overwritten, see FORMAT.md.
format: "buybutler-list/1"
id: 1
name: "Home"
currency: "EUR"
created: "2026-03-01T10:00:00.000Z"
updated: "2026-03-01T10:00:00.000Z"
created_by: "tom"
members:
  - {user: "tom", role: "owner", since: "2026-03-01T10:00:00.000Z"}
  - {user: "anna", role: "editor", since: "2026-04-11T08:30:00.000Z"}
---
```

`currency` is the three-letter code all amounts are in. `role` is `owner`,
`editor` or `viewer`.

## Deleted products and lists

When a product or a list is deleted in Buybutler, its folder is moved to
`trash/` with the date and time added to its name, and removed from there
after some days (`BUYBUTLER_TRASH_DAYS`). Its pictures are deleted at once,
so `photo` paths in the trash lead nowhere. Buybutler can't take a folder
back from the trash; it is there to look something up.

## What is not in this copy

The shopping list and the order of a store's aisles, who is logged in,
passwords, passkeys and access keys. They live in the database only: they
change all the time or are secrets, and they aren't needed to read what was
bought and how it was.
