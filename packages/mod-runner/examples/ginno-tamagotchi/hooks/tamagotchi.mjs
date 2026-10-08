// ginno-tamagotchi: a band pet fed by context usage (Ginno e2e sample).
// Exercises the P0 surface end to end: state slots + subscription redraw,
// the session.usage backend op, and the Button press round trip
// (ui.press → runner onPress → state.set → band redraw).
// Conceived after keras9496/claude-tamagotchi (an Electron desktop pet, not
// a mod — this is the band-renderable reading of the same idea).

const PET = { plugin: "ginno-tamagotchi", key: "pet" };

// hunger 0..3: fed recently → well fed; every turn without growth hungrier
const FACES = [
  ["(^x^)", "green", "full"],
  ["(^ ^)", "yellow", "content"],
  ["(o o)", "cyan", "peckish"],
  ["(T T)", "red", "starving"],
];

export function register(on) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    await feed($, 0);
    return result;
  });

  on("turn.complete", async ($, e, next) => {
    const result = await next(e);
    if (!e.agentId) {
      await feed($, e.usage?.context?.tokens ?? 0); // main-loop turns only
    }
    return result;
  });

  on("ui.render", { component: "AbovePrompt" }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e);
    const { value: pet = { fed: 0, hunger: 2, grew: 0 } } = await $.state.get(PET);
    const hunger = Math.min(3, Math.max(0, pet.hunger ?? 2));
    const [face, color, word] = FACES[hunger];
    const { Box, Text, Button } = $.ui.resolve(e);
    return Box({
      flexDirection: "row",
      paddingX: 1,
      gap: 1,
      children: [
        Text({ color, bold: true, children: face }),
        Text({ children: `pet ${word}` }),
        Text({ dimColor: true, children: `fed ${pet.fed ?? 0}× · grew ${pet.grew ?? 0}k` }),
        Button({ label: "feed", onPress: () => feed($, 1) }),
      ],
    });
  });
}

// One feeding: tokens grew → well fed and hunger resets; a press feeds a
// snack (hunger -1). Every state write redraws the band through the
// subscription the render pass registered.
async function feed($, grewTokens) {
  const { context } = await $.session.usage();
  const tokens = context?.tokens ?? 0;
  const grew = grewTokens > 0 ? Math.round(grewTokens / 1000) : Math.round(tokens / 1000);
  const { value: pet = { fed: 0, hunger: 2 } } = await $.state.get(PET);
  const hunger = grew > 0 ? 0 : Math.max(0, (pet.hunger ?? 2) - 1);
  await $.state.set(PET, { fed: (pet.fed ?? 0) + 1, hunger, grew });
}
