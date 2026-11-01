## What I Built

The Prompt Lab is a mini app that isolates prompts and responses for specific conversations with Claude. This allows us to examine how the structure of prompts can influence the responses generated. Conversations can be cleared so they do not influence subsequent conversations. The four tasks demonstrate different prompt techniques; zero-shot, one-shot, few-shot, and chain-of-reasoning. They provide opportunities to compare results from the different prompts and how they could be used in different circumstances.

## Why This Approach

Task 1 - Technique: zero-shot. This is useful for "brainstorming" when there is not a correct answer. It provides multiple answers that can be refined later. In this case, Wildframe Coffee Roasters was looking for ideas for new perks. Allowing multiple ideas to be generated is a useful technique to find new, creative ideas.

Task 2 - Technique: one-shot. This is useful when you have a specific format or layout that must be followed. In this case, the Wildframe Coffee Roasters have a specific format to describe each of their beans. Providing this format to Claude ensures each of them get consistent write-ups.

Task 3 - Technique: few-shot. This is useful when you are trying to match a style or "feel" that is difficult to describe in words. Providing multiple examples creates a theme that can be followed for responses to match. In this case, Wildframe Coffee Roasters wanted to generate answers to multiple questions or statements that all share a similar "voice" when providing responses.

Task 4 - chain-of-thought. This is useful when you would like to consider multiple factors and ask the agent to reason through a response. In this case, Wildframe Coffee Roasters wanted to get a recommendation on where to setup a pop-up while considering cost, type of customer base, and location based on previous data.

## Example Run

Zero-Shot Example:

claude-sonnet-5

### You

You are a copywriter for Wildframe Coffee Roasters, a coffee roaster that sells beans online and through a monthly subscription. Our brand voice is
warm, plainspoken, and a little irreverent, with no corporate jargon. Our
customers are environmentally conscious urban professionals.
We're creating subscriber perks: new perks for subscribers that stay six months or longer.
Write six perk ideas. Return a numbered list, for each perk generate one sentence of no more than 25 words.

### Claude

1. Hit six months and we'll send you a free bag of our rarest single-origin, no strings, just good beans.

2. Loyal subscribers get first dibs on new roasts before they ever hit the website or anyone else's mug.

3. Stick around half a year and we'll plant a tree in your name—because your coffee habit should give back too.

4. Six-month members score a discount code for our reusable steel canisters, so you can finally ditch the bags for good.

5. After six months, unlock free shipping for life—because loyalty shouldn't cost you extra, ever.

6. Hang in there six months and we'll invite you to our virtual cupping sessions, nerding out over beans together.

## Reflection

This exercise helped me recognize different ways to approach prompting based on what I wanted to accomplish or the results I was hoping to produce. I see the four techniques as being the most logical "foundation" for prompting because they could be used on the most common types of requests. I was impressed with how thorough and natural the responses were based on simple, direct requests. The zero-shot request was the most interesting because with a very basic request it returned unique suggestions that opened up my perspective on possibilities. I naturally gravitate toward a narrow-band of "styles", but asking the agent to provide ideas is very helpful to getting fresh ideas. The choice that I am the least sure of is the few-shot because I imagine it would require closer attention to make sure the content of the response is consistent with what I would want to say. In this example, it did exactly what I wanted, provided feedback in a voice that is consistent with the brand, but in real-world application I am sure I would have to be careful that 'what' it is saying is correct and just does not sound right. Perhaps a combination of chain-of-thought (to consider what the customer comment is actually saying or asking) used in conjunction with few-shot to craft a response in the right tone, would be a good option.
