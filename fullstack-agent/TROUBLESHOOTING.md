# Troubleshooting

This file covers only the problems that live BETWEEN the pieces. Each piece owns its own deeper guide: `ai-memory-vault/TROUBLESHOOTING.md`, `backtalk/TROUBLESHOOTING.md`, `barehands/TROUBLESHOOTING.md`, `ai-visualizer/TROUBLESHOOTING.md`.

## I closed the window in the middle of setup

Nothing is lost. Open a new terminal (PowerShell on Windows), go back to the toolbox folder (`cd ~/my-agent/fullstack-agent`, or on Windows `cd $HOME\my-agent\fullstack-agent`), and run:

```
claude --continue
```

That reopens your most recent session with its memory intact; tell it "we got cut off, keep going with the setup." If it can't find a session to continue, run `claude "set me up"` instead: the installer starts over, finds everything already downloaded, and skips ahead instead of redoing it.

## The install command opened Claude Code, but it acts like nothing's there

Then the download step failed before Claude Code started, and the error is in your terminal scrollback, right above where Claude opened. Type `/exit`, scroll up, and read it. On a Mac, a "developer tools" dialog may be waiting for an Open/Install click (that installs git; click Install and paste the command again). On Windows the command downloads a zip and needs no git, so a failure there is usually network. Fix what the message says, then paste the install command again.

## Windows says "claude is not recognized," or the Claude Code install "isn't doing anything"

Both are the same story. The Claude Code installer on Windows (the [start page](https://jaredrhod.com/start) command) downloads about 330 MB and prints nothing while it does: no progress bar, just a blinking cursor, for a few minutes, longer on slow wifi. People close the window because it looks dead, and then nothing is installed, so the next paste says `claude` is not recognized. Paste the start page command again and leave the window alone until it prints "Installation complete!" and then "All set." Then come back here and paste the install command again. It is safe to re-run: it skips the download it already did.

## The Mac says "xcrun: error: invalid active developer path"

Your Mac is missing Apple's Command Line Tools, which git needs. One command fixes it: run `xcode-select --install` in the same terminal, click Install on the popup, wait the few minutes it takes, then paste the install command again. This also shows up on Macs that recently upgraded macOS, because the upgrade can clear the tools; the same command puts them back.

## Claude opened a welcome screen (or asked me to log in) instead of setting up

Then this is your first-ever launch of Claude Code, and it runs its own one-time setup before anything else can happen: pick a text style, choose "Claude account with subscription" as the sign-in method (not the Console option, that's pay-per-use developer billing), and log in through your browser. Your "set me up" from the install command didn't survive that detour. No harm done: once you're signed in, paste the install command again and the wizard starts talking.

## The face sits at idle while the voice talks

The wiring is one config line, plus a restart. Check both:

1. `ai-visualizer/ai-visualizer.json` should have `"bus_dir"` pointing at your backtalk folder. (The same wire can run from the other side instead: `"signals_dir"` in `backtalk/backtalk.json` pointing at the visualizer folder. One direction, not both.)
2. Restart the visualizer server after any config change (Ctrl-C the stack, run start.sh again). Config edits only take effect on restart.

While the agent speaks, the backtalk folder should contain fresh `.voice_state` and `.voice_waveform` files. If they are not appearing, the problem is on the voice side; work backtalk's own guide.

## The greeting doesn't speak on launch

The greeting line lives in `backtalk/backtalk.json` under `"greeting"`. If it is missing or empty, the launch is silent by configuration. The voice piece itself failing to start is a different problem; its terminal output says why, and its guide covers the classics.

## start.sh says a piece is starting but nothing appears

- The face opens a browser tab automatically, on whichever face your `ai-visualizer.json` names. If no tab appears, open `http://127.0.0.1:8790/` yourself and click your face from the gallery. That address is the picker, not a face, so going straight there and expecting the animation is the usual confusion.
- The hands never open a tab automatically (the camera page should be opened deliberately): `http://127.0.0.1:8794/` in Chrome.
- Two stacks can't run at once. If a port is already busy from an earlier session, Ctrl-C the old terminal or close it, then start again.

## My agent forgot who it is

Your agent's identity lives in the `CLAUDE.md` in your HOME folder (the folder containing all the tool folders), and Claude Code only reads it when you open Claude Code IN that folder. Opening Claude Code inside one of the tool subfolders boots the tool's own instructions instead. Daily habit: work from the home folder.

## I moved my agent folder somewhere else

Everything is wired with paths, so a move breaks the wires. Open Claude Code in the new location and say: "read fullstack-agent/fullstack-agent.md and re-run the wiring phase." Rewiring takes a minute and touches only the config paths.

## Updates

`./fullstack-agent/update.sh` pulls every piece. Your files (your CLAUDE.md, your vault, your notes) are never inside the repos' tracked files, so updates cannot touch them. If git complains about a config file you edited (backtalk.json, ai-visualizer.json), your edit wins; keep your version.
