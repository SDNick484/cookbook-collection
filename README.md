# Cookbook Collection

A family cookbook catalog: Flask + SQLite, running as one small service.
On the server it's called `cookbooks`: the service, `/opt/cookbooks`, `/var/lib/cookbooks`, `/etc/conf.d/cookbooks`, and port 8767.

- **Public** `https://cookbooks.example.com/` lets visitors browse and search books and recipe names.
- **Editors** `https://cookbooks.example.com/admin` sit behind Cloudflare Access. From there you can add books by barcode, edit them, import indexes, and see ratings, notes, shelf location and the want-to-try list.

## Install / upgrade

The installer targets **Alpine Linux** with OpenRC, e.g. a small container or VM. Build the one-file installer from this repo, copy it to the server, and run it as root:
```sh
sh installer/build.sh                 # -> dist/setup-cookbooks.sh
scp dist/setup-cookbooks.sh root@<server>:/root/
ssh root@<server> sh /root/setup-cookbooks.sh
```
The script is safe to run again. It replaces the app but keeps `/var/lib/cookbooks` (the database and covers) and `/etc/conf.d/cookbooks`. `sh setup-cookbooks.sh --uninstall` removes the app and service and keeps the data.

On a first install the database is seeded from `seed.json`, which holds the original collection's books and recipe names. To start your own catalog empty, replace `seed.json` with `{"books": [], "recipes": []}` before building.

Health alerts post to a Home Assistant webhook. To use them, build with `HA_WEBHOOK_ID=… sh installer/build.sh`, or set `HA_WEBHOOK_ID` in `/etc/conf.d/app-ops` after installing.

## Cloudflare (hybrid public + private)

1. **Tunnel → Public hostname**: `cookbooks.example.com` → `http://<server IP>:8767`.
2. **Access → Applications → Add → Self-hosted**:
   - Domain `cookbooks.example.com`, **path `admin`**. Leave the root unprotected so the catalog stays public.
   - Policy: Allow → Emails → the people who should be able to edit, e.g. with a One-time PIN login.
   - Copy the app's **Application Audience (AUD) tag**.
3. On the server, edit `/etc/conf.d/cookbooks`. Set `CF_TEAM_DOMAIN` (e.g. `yourteam.cloudflareaccess.com`), `CF_AUD`, and optionally `ADMIN_EMAILS`. Then run `rc-service cookbooks restart`.

The path rule covers `/admin` and everything under it, including the `/admin/api/*` calls that do the editing. The app also verifies the Access JWT itself, so an unsigned request that reaches it through the tunnel can't edit. Requests that don't come through Cloudflare, i.e. directly on the LAN, can edit while `LAN_EDIT=1`.

## Adding books
**Add book → Scan barcode** uses the phone camera. This needs Chrome on Android over HTTPS, so use the public HTTPS address rather than the LAN IP. The ISBN is looked up on Open Library, then Google Books, and the title, authors, publisher, year and cover are filled in.

For older books with no barcode, type the title and use **Find matches by title**.

## Importing a book's index
Open a book, choose **Import index**, and paste the index text. Google Lens on Android can copy text straight from a photo of the index page. You get an editable preview before anything is saved, and re-importing the same page skips duplicates.

## Photo index import
Set `ANTHROPIC_API_KEY` in `/etc/conf.d/cookbooks` and restart the service. **Import index → Photos of index pages** sends each photo (grayscale, ≤1568px) to Claude, which returns the entries as structured rows for the usual preview.

## Dinner Log link (optional)
If Dinner Log, a companion meal-journal app, runs on the same server, Cookbook Collection reads `/var/lib/dinner-log/meals.db` **read-only**. The installer adds the `cookbooks` user to the `dinnerlog` group so it can.
- Every dinner logged with source "Cookbook" is matched to a book by title, then to a recipe by page number and name.
- Recipes show cooked count, average rating and last made. **Stats** shows the overall picture.
- When a name doesn't match, pick the book once on the Stats page. When a dinner matches the book but not a recipe, link it or add it as a recipe from the book's page.
- Dinner Log itself is unchanged.

## Eat Your Books import
**More → Import from Eat Your Books** accepts both of EYB's CSV exports.
- **Bookshelf:** books are matched by ISBN, then by title and author. You review the proposals, and can pick another book or skip any row. Matched books gain their ISBN, format and EYB title. Books you don't have here yet are added and land on Needs review. Re-importing is safe.
- **Recipes:** import EYB's 200-row exports one at a time, or all of them combined into one file. Recipes already present are skipped, including re-worded copies of recipes entered another way on the same page. Books not in the catalog can be picked, added as new, or skipped. EYB's title for each book is remembered, so later imports match on their own.
- **Fetch covers & details** then looks up every book with an ISBN on Open Library and Google Books. It fills in the cover, year, publisher and subtitle only where they're missing.

## Ingredient tags
EYB's ingredient search finds far more recipes than titles do: searching "chicken" there returns everything that uses chicken, stock included. To bring that in, search an ingredient on EYB, export the results, and import them under **More → Import from Eat Your Books → Ingredient lists** with the ingredient's name.
- Recipes already in the catalog get tagged; ones that aren't yet get added.
- The catalog shows a chip per ingredient. Picking one lists the books that use it, with how many matching recipes each has; open a book to see those recipes. Chips combine with search words, cuisine and tag, which narrow both the books and the counts.
- Search matches every word against recipe names *or* ingredient tags, so "chicken curry" finds curries that use chicken.
- The tags are a few major ingredients, not a full ingredient list. Only some recipes carry them, so picking an ingredient (a chip, or `ingredient` in the MCP) finds recipes tagged with it **or** with it in the title. A more specific ingredient uses the tag inside it: "ground beef" uses Beef, "egg" uses Eggs, "cherry tomatoes" uses Tomatoes. One with no tag, like "turkey", matches titles only. When two tags apply because one contains the other ("sweet potatoes" → Sweet potatoes and Potatoes), only the more specific one is used. Name tags with the fewest words people would type: "Collards", not "Collard greens". The MCP's `search_recipes` lists the tags and says which one it used.
- Re-importing a list is safe. Removing an ingredient deletes only the tag, not the recipes.
- **Ranking:** search results (on the site and in the MCP) come best first. A word in the recipe's title or index heading counts most, then a specific tag (Leeks, Chicken pieces), then a broad one. So for "chicken leeks", "Risotto with Chicken, Leek and Garlic" comes before dishes that only got there through tags. On the site, each book's recipes stay together, with the books holding the best matches first. Plural words also find singular titles ("leeks" finds "Leek and potato soup").
- **Minor forms:** EYB's list for an ingredient includes recipes that only use a minor form of it. Its "chicken" list includes recipes that only use chicken stock, "tomatoes" anything with a spoonful of tomato paste, and "rice" dishes seasoned with rice wine. Import those forms as hints named **<main tag> <form>**, where the form is stock, broth, fat, paste, sauce, vinegar, wine, flour or starch: Chicken stock, Tomato paste, Rice wine, Duck fat, Fish sauce, Bean paste… Each is recognized from its name. A recipe tagged with the main tag *and* one of its minor forms, with no mention in its title and no more specific tag, then ranks low, because it may use only that form. It isn't dropped, since most chicken soups use both meat and stock. A search that names the form ("tomato paste") ranks those recipes normally. Several EYB searches can feed one tag: type the same name when importing "white stock" and "chicken broth" as Chicken stock. Don't name a tag this way unless you want the demotion; a "Tomato sauce" hint would demote tomato dishes. To mark recipes that use the meat itself, import the cut lists (breasts, thighs, wings…) under one hint such as **Chicken parts**.
- **Search hints:** for everyday ingredients like zucchini, kale or collards, tick **Search hint only** when importing; it's ticked for a new tag. A hint tags recipes just like Beef or Chicken. Searching "zucchini", the ingredient filter and Claude (MCP) all find them, but it gets no chip on the catalog page. The MCP lists hints separately from the major tags. **Show chip / Hide chip** in the ingredient list switches a tag between the two kinds.
- If an export's accented letters were lost before import ("tud�usi" for "tudòusi"), the rows are recognized as recipes already in the catalog instead of being added again. Garbled copies already in the catalog are merged into their correctly spelled twin on the same page when the app starts, keeping their tags, stars, notes and Dinner Log links. Ones with no twin are listed on **Needs review** to retype.

## Recipes from websites (NYT Cooking and others)
Recipes from sites like NYT Cooking or Serious Eats are saved the way a cookbook's index is: a name and a link. Each site gets its own entry in the catalog, made the first time one of its links is saved. Opening a recipe takes you to the site, where you're signed in to your subscription.
- **More → Recipes from websites:** paste one link or many, in any text. You get a preview with editable names before anything is saved. Links that aren't recipe pages are skipped, and links already saved are recognized even with tracking parameters added.
- **Whole NYT Cooking Recipe Box:** drag **Copy recipe links** from that page to your browser's bookmarks bar. Open your Recipe Box and scroll to the bottom so it's all loaded, then click it and paste the result. It only reads links already on the page, in your own browser.
- **From your phone:** install the editor page to your Android home screen (Chrome → Add to Home screen). **Share → Cookbook Collection** from the NYT Cooking app or a recipe page then opens the preview with that link, using the site's own title.
- **Names:** no page is downloaded from the site, so names come from the link ("1017256-french-onion-soup" → "French Onion Soup"). Fix any in the preview, or later with ✎.
- **Where they show up:** web recipes appear in search, ingredient chips (by title), Want to try and Cook suggestions, with **Open ↗** instead of a page number. Dinner Log dinners whose source holds the recipe's link count as cooking it.
- **Editors only:** online sources aren't shown in the public catalog, public search or the public MCP. They aren't counted as cookbooks either; Stats shows them separately.

## Family wish list (optional)
If a companion wish list app runs on the same server, set a book's status to **Want to buy** and use **Add to a wish list…** on its page. It adds the book to the chosen person's list with a shopping link built from the ISBN (`WISHLIST_BOOK_URL`). It does this through the wish list app's own API on localhost, so the wish list itself is unchanged. Purchase status is never shown here, so there are no spoilers.

## Dinner Log ⇄ Cookbook Collection
- **In Dinner Log** (updated `setup-dinner-log.sh`): choosing a cookbook offers that book's indexed recipes, and picking one fills in the page and the dinner name. The data comes from Cookbook Collection over localhost, and the form works as before if it's down.
- **In Cookbook Collection:** set `DINNERLOG_URL` and each recipe gets a 🍽 link that opens Dinner Log's form already filled in.

## Accents and other alphabets
- Search ignores case and accents, and also special letters: "creme brulee" finds *Crème brûlée*, "pepin" finds Jacques Pépin, "oeufs" finds *Œufs*, "smorrebrod" finds *Smørrebrød* and "strasse" finds *Straße*. The same applies to ingredient tags, the filter on a book's page and Dinner Log title matching.
- Chinese, Japanese, Korean and Thai don't put spaces between words, so a search in those scripts matches anywhere in a recipe name or book title.
- Authors, tags and ingredients that differ only by accents or case are treated as one. "Jose Andres" from an EYB export joins an existing "José Andrés", and an accented spelling replaces an unaccented one.
- Saved text is normalized, so an "é" typed on a Mac matches one from a CSV, and ﬁ/ﬂ ligatures from PDFs or photographed indexes are spelled out.
- The EYB import repairs text garbled by a round-trip through Excel ("CrÃ¨me" → "Crème"). **Download CSV** includes a byte-order mark so Excel opens accents correctly.
- The search index stores folded text, using a `fold()` SQL function the app registers on every connection. It's rebuilt on each start. Edit recipes through the app rather than the `sqlite3` command line, which doesn't have `fold()`.

## Book ratings
Editors can give each book an overall rating of 0–5 stars, in half stars.
- On a book's page, tap the left half of a star for a half star or the right half for a whole one. The arrow keys also change it, and **Clear** removes it.
- The **Edit book** form has the same rating as a menu.
- Ratings appear on the catalog cards, and **Top rated** sorts by them, with unrated books last. Like recipe ratings, they're visible only to editors, not the public catalog.
- The rating is also in the CSV export and in the private MCP's book results.

## Photos of what you made
Editors can attach photos of a dish to the recipe it came from.
- On a book's page, tap **📷** on a recipe row and choose **Add photos**. On a phone this offers the camera or the gallery, and you can pick several at once.
- The date is read from each photo. Set **Cooked on** to override it, and add an optional caption.
- The book's page shows every photo under **What we've made**, with the recipe name and page. Tap one to view it full size, and use ‹ › or the arrow keys to step through them. The 📷 on a recipe row shows how many photos it has.
- Photos are editor-only, like ratings and cooking history. They're served from `/admin/photos/`, so Cloudflare Access covers them, and they don't appear in the public catalog.
- Each upload is resized to at most 2048px, plus a 480px thumbnail, and saved as JPEG in `/var/lib/cookbooks/photos`. Re-encoding drops the EXIF data, including any phone GPS location.
- Deleting a photo, recipe or book deletes its files. The nightly backup copies `photos/` to `BACKUP_TARGET`, and **Download JSON** lists each recipe's photo files.
- iPhone HEIC photos work if the `pillow-heif` Python package is installed. Without it, Safari normally converts them to JPEG when uploading.

## What should we cook? / Stats / shelf
- **Cook** gives random ideas in four modes: starred but not yet made, never made, forgotten books, and old favorites.
- **Pulled out / Put back** on a book's page tracks books that are off the shelf.
- **Stats → The collection** counts the books you own by author, publisher, tag, book rating, decade published and format. Publisher spellings that differ only by case or accents are counted together. Each list shows the top 12, with the rest under **All**, and tapping an author, publisher or tag opens those books in the catalog.

## MCP (use the catalog from Claude)
- `https://cookbooks.example.com/mcp` is public and read-only: search books and recipes, get a book with its index.
- `https://cookbooks.example.com/mcp/<MCP_TOKEN>`, or a `Bearer <MCP_TOKEN>` header, adds ratings, cooking history, `suggest_recipes`, `cooking_stats` and `set_want_to_try`.
- The token is generated on install and lives in `/etc/conf.d/cookbooks`. Treat the private URL like a password.
- `/mcp` must stay **outside** the Access app, which only protects `/admin`.
- `search_recipes` returns the best matches first, the `total`, and `next_offset` when there are more; each recipe's `match` says whether it matched on the title or on tags, and flags "may use only chicken stock". The server's instructions tell Claude that some books have no page numbers and that the recipe and book title are enough.

## Backups and alerts
The ops scripts cover Cookbook Collection and, if they're installed on the same server, Dinner Log and the wish list. Their settings live in `/etc/conf.d/app-ops`.
- `/etc/periodic/daily/app-backup` takes consistent SQLite snapshots into `/var/backups/apps`, kept `KEEP_DAYS` days.
- Set `BACKUP_TARGET` to a mounted folder on other storage, e.g. an NFS share from a NAS. Snapshots plus photos and covers are copied there too.
- `/etc/periodic/15min/app-health` checks each app. After ~30 minutes down it sends one alert to a Home Assistant webhook automation, and one more on recovery. Set `HA_URL` and `HA_WEBHOOK_ID` to turn alerts on.
- The app runs under `supervise-daemon`, so a crash restarts it automatically.

## Backup / restore by hand
Copy `/var/lib/cookbooks/` (`cookbooks.db` + `covers/`), or use **More → Download JSON**.

To restore a snapshot, stop the service and decompress the snapshot over the database:
```sh
rc-service cookbooks stop
gunzip -c /var/backups/apps/<date>/cookbooks-cookbooks.db.gz > /var/lib/cookbooks/cookbooks.db
rc-service cookbooks start
```

## Development
```sh
python3 app.py                      # http://localhost:8767, data in ./data
python3 -m pytest -q tests
```
