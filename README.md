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

Health alerts post to a Home Assistant webhook. To use them, build with `HA_WEBHOOK_ID=… sh installer/build.sh`, or set `HA_WEBHOOK_ID` in `/etc/conf.d/ct210-ops` after installing.

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
- Re-importing a list is safe. Removing an ingredient deletes only the tag, not the recipes.

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

## What should we cook? / Stats / shelf
- **Cook** gives random ideas in four modes: starred but not yet made, never made, forgotten books, and old favorites.
- **Pulled out / Put back** on a book's page tracks books that are off the shelf.

## MCP (use the catalog from Claude)
- `https://cookbooks.example.com/mcp` is public and read-only: search books and recipes, get a book with its index.
- `https://cookbooks.example.com/mcp/<MCP_TOKEN>`, or a `Bearer <MCP_TOKEN>` header, adds ratings, cooking history, `suggest_recipes`, `cooking_stats` and `set_want_to_try`.
- The token is generated on install and lives in `/etc/conf.d/cookbooks`. Treat the private URL like a password.
- `/mcp` must stay **outside** the Access app, which only protects `/admin`.

## Backups and alerts
The ops scripts cover Cookbook Collection and, if they're installed on the same server, Dinner Log and the wish list. Their settings live in `/etc/conf.d/ct210-ops`.
- `/etc/periodic/daily/ct210-backup` takes consistent SQLite snapshots into `/var/backups/ct210`, kept `KEEP_DAYS` days.
- Set `BACKUP_TARGET` to a mounted folder on other storage, e.g. an NFS share from a NAS. Snapshots plus photos and covers are copied there too.
- `/etc/periodic/15min/app-health` checks each app. After ~30 minutes down it sends one alert to a Home Assistant webhook automation, and one more on recovery. Set `HA_URL` and `HA_WEBHOOK_ID` to turn alerts on.
- The app runs under `supervise-daemon`, so a crash restarts it automatically.

## Backup / restore by hand
Copy `/var/lib/cookbooks/` (`cookbooks.db` + `covers/`), or use **More → Download JSON**.

To restore a snapshot, stop the service and decompress the snapshot over the database:
```sh
rc-service cookbooks stop
gunzip -c /var/backups/ct210/<date>/cookbooks-cookbooks.db.gz > /var/lib/cookbooks/cookbooks.db
rc-service cookbooks start
```

## Development
```sh
python3 app.py                      # http://localhost:8767, data in ./data
python3 -m pytest -q tests
```
