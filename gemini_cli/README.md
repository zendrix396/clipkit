# gemini_cli

Gemini browser workflow for login and image generation.

## Commands

```powershell
gemini-cli login --email "you@gmail.com" --profile-dir gemini-profile
gemini-cli image "a minimal product render on a white desk" --profile-dir gemini-profile
gemini-cli image "a concept image inspired by the upload" --reference-image .\reference.png --profile-dir gemini-profile
```

## Common Options

```powershell
gemini-cli login --profile-dir gemini-profile --browser-binary "C:\Program Files\Google\Chrome\Application\chrome.exe"
gemini-cli image "prompt" --profile-dir gemini-profile --headless
gemini-cli image "prompt" --profile-dir gemini-profile --reference-image .\ref1.png --reference-image .\ref2.jpg
```

## Output

- `gemini-cli-output/login-*` for sign-in screenshots and login metadata
- `gemini-cli-output/image-*` for generation screenshots, result JSON, and downloaded images

## Flow

1. Log in once with a persistent profile.
2. Reuse the same profile for later generations.
3. Keep reference images local and explicit.

## Notes

- `GEMINI_GOOGLE_EMAIL` and `GEMINI_GOOGLE_PASSWORD` are read from the environment when present.
- `--manual-password` is available when you want to finish sign-in yourself.
- `--reference-image` can be repeated.

