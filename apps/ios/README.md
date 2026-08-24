# PDX Train for iPhone

One screen and a widget: red or green at 12th & Clinton, at a glance.
The widget is the product - home screen and lock screen aspects that update on
WidgetKit's budget (every 5-15 minutes, matching the cameras' own cadence).
The app is the widget's home: the flasher big enough to read across a room,
the blocked ticker, and the same ODOT-vs-us honesty note the website shows.

## One-time setup

1. Install Xcode from the App Store (the command-line tools are not enough),
   then point the tooling at it:
   `sudo xcode-select -s /Applications/Xcode.app`
2. `brew install xcodegen`
3. From this directory: `xcodegen generate && open PDXTrain.xcodeproj`
   (The `.xcodeproj` is generated and gitignored; `project.yml` is the truth.)
4. In Xcode, select the `PDXTrain` target -> Signing & Capabilities -> choose
   your team for ALL FOUR targets - PDXTrain, PDXTrainWidget,
   PDXTrainWatch, PDXTrainWatchWidget - or the install fails.
   - A free Apple ID works but the install expires after 7 days and needs a
     re-run from Xcode.
   - The $99/year developer account removes that dance (and enables
     TestFlight installs from your phone with no cable).
5. Plug in the phone (or pick it under Devices over Wi-Fi), press Run.
6. Add the widget: long-press the home screen -> Edit -> Add Widget ->
   PDX Train. The lock-screen variants are under Customize Lock Screen.
7. Siri works immediately with the built-in phrases ("Hey Siri, PDX Train
   status" or "Is the train blocking in PDX Train" - Apple requires the app
   name in built-in phrases). For the natural wording: open the Shortcuts
   app -> + -> add the "Check the crossing" action -> rename the shortcut to
   "Is the train currently blocking" - that exact phrase now works with Siri,
   and Siri speaks the answer either way ("Yes - a train has been blocking
   12th and Clinton for 24 minutes").

## The watch

The watch app embeds in the phone app, so installing the phone app offers the
watch app on the paired Watch automatically (or install it from the Watch app
on the phone). Add a complication: long-press the watch face -> Edit ->
Complications -> pick a slot -> PDX Train. Every form carries the state in
something other than color - a per-aspect glyph (train / checkmark / question
mark) on the circular forms, the aspect word or a labeled duration on the
rest: watchOS renders complications in accented mode on tinted faces, where
hue flattens to a single tint and a red-vs-green dot would say nothing.

Bundle IDs are nested deliberately (`com.alexnorum.PDXTrain`, `.Widget`,
`.watchkitapp`, `.watchkitapp.widget`): iOS refuses to install an extension
whose id is not prefixed by its parent app's, which the simulator reports as
"Mismatched bundle IDs".

## Layout

- `project.yml` - XcodeGen spec: four targets - the iOS app and its widget
  extension (iOS 17+), plus the watch app and its complication extension
  (watchOS 10+). Bundle ids are pinned there, not derived.
- `Sources/Shared` - the wire contract (`/api/v1/status` reduced to a
  glance), the board's colors, the twin-lamp Flasher view, and the timeline
  entry and provider both widget extensions share. Compiled into all four
  targets.
- `Sources/App` - the one-screen SwiftUI app: the crossing on a muted dark
  map, its own flasher as the pin, the aspect on a plaque below. The camera
  position is state and the pin is a plain `Annotation`, so adding "you are
  here" later is `UserAnnotation()` plus a location-usage string.
- `Sources/WatchApp` - the watchOS app (aspect, ticker, nothing else).
- `Sources/WatchWidget` - watch-face complications: circular, corner,
  rectangular, inline.
- `Sources/Widget` - the WidgetKit timeline and views.

No secrets, no accounts, no write access: the app is a read-only client of
the public board, and deleting it forgets nothing.
