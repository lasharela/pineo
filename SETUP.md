# Pineo setup: what the owner does by hand

Everything below is done once, in the project root (`pineo/`). The Python venv
(`.venv/`, with `bleak` and `pyserial` for Bluetooth and USB) is already made.

1. **Make your `.env`.** `cp .env.example .env`. It is git-ignored; check with
   `git status` that `.env` is not listed. Write each value after its `=`, no spaces, no quotes.
2. **`DEEPGRAM_API_KEY`**: your team's Deepgram key, from the hackathon organisers.
3. **`MINIMAX_API_KEY`**: claim the $30 credit with the form in the hackathon Discord
   sponsor channel, then create an API key in the MiniMax OpenPlatform console
   (https://platform.minimax.io, international). `MINIMAX_BASE_URL`,
   `MINIMAX_ANTHROPIC_BASE_URL` and `MINIMAX_MODEL` are already filled with the defaults
   from MiniMax's docs. The GroupId from the console is optional (the OpenAI- and
   Anthropic-compatible APIs do not need it); fill `MINIMAX_GROUP_ID` only if asked.
4. **`PINEO_BAND_NUMBER`**: the number written on your band's bag (42 means the band is
   `Vexo-HACK-42`). Use only your own band's number.
5. **Optional `OPENAI_API_KEY`**: only if you want the kit's own OpenAI examples to work.
6. **Check the Deepgram key** (about 30 s, no band needed, uses the pretend band):

       .venv/bin/python vexo-open-kit/examples/python/check_keys.py

   It checks **Deepgram and OpenAI only, not MiniMax**. With no OpenAI key, its OpenAI
   line says `NOT working`: that is expected for Pineo. The Deepgram line must say `works`.
   The table of errors is in `vexo-open-kit/AGENTS.md`, "Check both keys".

Never paste a key into a chat (also not to a coding agent). If a key leaks, tell an
organiser at once.
