# First test guide

For a tester installing AVE Studio on their own Mac. You need an Apple Silicon Mac on macOS 14 or newer, about 2 GB free for the app, and room for your footage.

## 1. Install

1. Open the DMG and drag AVE Studio to Applications.
2. Open it from Applications and choose or create a workspace.
3. Open Terminal and run:

   ```sh
   "/Applications/AVE Studio.app/Contents/MacOS/AVE Studio" --install-cli
   ```
4. Run `~/.local/bin/ave doctor`. It should end with READY. Warnings about whisper, librosa, OpenCV and publishing are expected.

## 2. Make something small

Start with a short recording of your own, 5 to 10 minutes. In Claude Code, open a new folder and ask:

> Use the AVE skills. Start an episode from this recording and take it to a rough cut. Stop at each human checkpoint.

Your agent does the labor. Approvals and trims stay yours.

## 3. See it in Resolve (optional)

Install DaVinci Resolve 21.0.x and Python 3 from python.org. Restart Resolve, open a new project, and choose Workspace > Scripts > resolve_bridge. Then ask your agent to hand off the cut.

## 4. Find your way around

- **The Workspace** is the board of everything you are making, with Calendar, Previews, Sources, Approvals and Jobs.
- **The Room** is where you watch a cut with Claude: Cut, Script and Selects for video, Layout for a post. Flag a moment and say what to change.
- **Settings** holds Accounts, Alerts, People and Collaboration.
- **More tools** (in the Workspace header) opens Production, Media tools, the Feed planner and the Post editor in their own window.

## 5. Share a project with your collaborator

1. Install Tailscale from tailscale.com (not the App Store) and join the shared tailnet.
2. In Settings › People, choose Share on a project and follow the sheets. The other person chooses Join a shared project.
3. Compare the six-digit code on a call, then both confirm.
4. Watch the project arrive on the other computer: its footage and versions sync while you are both online. Interrupt the connection partway and confirm it picks up again.
5. To send a single draft to someone outside the project, use Settings › Collaboration.

## 6. Tell us

Note what confused you, what broke, and the exact wording of any error.
