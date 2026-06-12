# image_search_cli

Google Images asset gatherer for fast source collection.

## Command

```powershell
image-search-cli "blue glass cube product photo"
```

## Common Options

```powershell
image-search-cli "query" --limit 5
image-search-cli "query" --headless --headless-new
image-search-cli "query" --profile-dir gemini-profile
image-search-cli "query" --browser-binary "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

## Output

- `image-search-output/search-* / image-tab.png`
- `image-search-output/search-* / results.json`
- `image-search-output/search-* / images/image-01.*`

## Flow

1. Open Google Images.
2. Collect the top results.
3. Save the page screenshot and downloaded assets.

## Notes

- Use a logged-in profile if Google shows an unusual-traffic page.
- Increase `--limit` only when you need more source candidates.

