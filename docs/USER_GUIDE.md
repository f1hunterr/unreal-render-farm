# How to Use the Unreal Render Farm

*A step-by-step guide for artists, in simple English.*

The render farm lets other computers render your Unreal shots for you, so your own computer stays free.
You fill in one form on a website, press one button, and the pictures appear in your folder.

---

## Before you start

You need 3 things. If one is missing, ask your farm admin.

1. **The farm's web address and password.** For example `http://192.168.1.5:5000`, user name `admin`.
   Your farm admin gives you the password.
2. **Your project on the shared drive.** The render computers must be able to open your project, so it
   must live on the shared network drive, not only on your own computer.
3. **A render preset that saves pictures.** In Unreal's Movie Render Queue, your preset must save
   **pictures** (EXR, PNG or JPG). You may add a video too, but the pictures are what counts.

One-time setup for each project: in Unreal, open **Edit → Plugins**, search for
**Python Editor Script Plugin**, tick it, and restart Unreal. The farm needs it to talk to Unreal.

---

## Step 1: Open the farm and log in

1. Open Chrome or Edge on any computer in the studio.
2. Type the farm's address in the address bar and press **Enter**.
3. A small box asks for a name and password. Type `admin` and the password, then click **Sign in**.

At the top are four tabs: **Dashboard** (start renders, see the computers), **Queue** (what is waiting),
**History** (what is finished) and **Admin** (for the farm admin only).

## Step 2: Tell the farm about your project

On the **Dashboard** tab, scroll down to **Send a Render** and fill in the top 3 boxes. The farm remembers
them for next time.

![The Send a Render form](images/job-form.png)

| Box | What to put in it | How to copy it |
|---|---|---|
| **Project file (.uproject)** | Your project on the shared drive, e.g. `N:\Projects\Film\Film.uproject` | In File Explorer, hold **Shift**, right-click the `.uproject` file, choose **Copy as path** |
| **Map (level)** | The level to render, e.g. `/Game/Maps/Main.Main` | In Unreal's Content Browser, right-click the level, choose **Copy Reference** |
| **Render preset** | Your Movie Render Queue preset, e.g. `/Game/Cinematics/Final.Final` | Right-click the preset, choose **Copy Reference** |

Just paste: extra quotes or the long text Unreal copies are fine, the farm cleans them up.

Then check two settings:
- **Priority:** **Normal** for everyday work. **Rush** jumps the line, so only use it when it's really urgent.
- **Retries if it fails:** keep **2**. If a render breaks, the farm tries again by itself, on another computer if one is free.

**Want the pictures in a special folder?** Open **Advanced** and type it in **Save frames to**, for example
`K:\Renders\MyShot`. Leave it empty to use the farm's folder. Always use a folder on the shared drive.

## Step 3: Add your shots

Under **Shots**, each row is one shot.

1. Paste your Level Sequence into the long box (right-click it in the Content Browser → **Copy Reference**).
2. The green **Ready** button means the shot will be rendered. Click it to **Skip** a shot for now.
3. More shots? Click **Add Shot**.
4. Under **Computers to use**: green = free, blue = rendering, red = offline. Ticked computers may be used.
   Usually leave them all ticked.

## Step 4: Long shots are shared automatically

The box **Share each shot between free computers** is ticked for you. The farm then splits each shot into
one piece per free computer, and they all render at the same time into the same folder. With 4 computers,
a long shot finishes about 4 times faster.

- Short shots (under 100 frames) stay on one computer.
- If your preset also saves a **video**, each computer keeps the pictures and skips the video. Make the
  video from the pictures afterwards.
- **Untick the box** only for shots with cloth, destruction or long particle effects that build up over time.
- Want only part of a shot? Type its frames in the small **Frames** box, e.g. `0-99`.

## New project? Prepare it first

The first time a computer opens a project, Unreal spends a long time getting it ready ("Building meshes",
"Compiling shaders"). **Prepare project first** (under the Send button) does that work once, so every
computer starts fast afterwards. Click it, then click **Send to Render Farm** straight away: your render
waits until the preparing is done.

## Step 5: Press the big button and watch

1. Click **Send to Render Farm**. A message in the corner says how many shots were sent.
2. Free computers start by themselves. You can close the website: the farm keeps working.

**On the Dashboard**, each computer has a card. While it gets ready it says what it is doing, for example
**Building meshes 54/445** or **Warming up 3/8**. Once pictures come out it shows the frame, percent and
time left (the time left gets accurate after a few frames).

![Two computers rendering at the same time](images/dashboard.png)

**On the Queue tab**, every shot or piece is one row. Shared shots also get one bar under **Shared Shots**.
To stop one, click **Cancel**. **Clear finished** tidies the list (the History tab keeps everything).

**Where are my pictures?** Look for the small folder line (📂) on the computer's card, under the shot on the
**Queue** tab, and on the **History** tab. Click **Copy**, then paste it into File Explorer's address bar.
It's the farm's output folder (if your admin set one), or else your preset's folder. Finished jobs also
show how many seconds each frame took.

## Your own PC can help (workstation mode)

Your admin can set your PC up to render while you are away. It only takes farm work after 15 minutes
without keyboard or mouse, and **when you come back the farm render stops within seconds**. The shot is
finished on another computer. Leave your PC switched on (not asleep) before you go home.

---

## If something goes wrong

Open the **Queue** tab: a failed row says why under the shot name.

| The message says | Fix it like this |
|---|---|
| *project file not found on this node* | Put the `.uproject` on the shared drive and paste its path again |
| *must be an Unreal asset path* | Copy the path again with **Copy Reference** in Unreal |
| *without a result from the URF executor* | Enable the **Python Editor Script Plugin** (see Before you start) |
| *writes one video per task* | Your preset saves only a video. Add a picture output (JPG, PNG or EXR) |
| *frame file(s) missing or empty on the drive* | Pictures didn't save on the shared drive. The farm renders them again; if it repeats, the drive may be full |
| *Unreal froze* | Unreal got stuck; the farm closed it and tries again |
| *ran out of memory* | The farm tries another computer. If not, use **Edit** to tick more computers or lower the preset |
| *GPU Crashed or D3D Device Removed* | The graphics card crashed; the farm retries. If it repeats, tell your admin (driver or heavy preset) |
| *reported success but wrote no files* | Check the preset has a picture output on and your frames are inside the shot |
| *can't render this; waiting for another computer* | One computer can't open your project; another one takes it |

**To try again:** **Retry** (same settings) or **Edit** (see why it stopped, change settings, add computers,
then **Retry with these settings**).

## Golden rules

**Do:** keep projects and output on the shared drive · save pictures · check a shot plays in Unreal before
sending · use Normal priority unless it's urgent.

**Don't:** render from your own Desktop or `C:` drive · split shots with cloth, destruction or long particle
effects · share the farm password outside the studio.
