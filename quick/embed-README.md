# Embedding the chat agent and dashboard on a supervisor page

`site/index.html` is a static supervisor page with two slots: the compliance dashboard on the left and the chat agent on the right, plus a small station header. It carries no credentials; AWS Quick issues the embed content.

## Steps

1. **Allow your domain.** In the AWS Quick administration console, add the domain that will host the page to the allowed domains for embedding (Manage account, then Domains and embedding). For a quick test, host `site/index.html` from an S3 bucket behind CloudFront, or any web server you control, and allow that domain.
2. **Dashboard embed.** Open the dashboard created by `./dashboard.sh`, choose Share, then Embed (or use the `GenerateEmbedUrlForRegisteredUser` API for a registered user, or the anonymous variant with a capacity pricing plan). Copy the embed snippet.
3. **Chat agent embed.** Open the chat agent, choose Share, then Embed, and copy the snippet. If your Quick account exposes the embed through the API instead, generate the URL there.
4. **Paste both snippets** into `site/index.html` where the comments say `DASHBOARD EMBED` and `CHAT EMBED`.
5. **Publish** the page to the allowed domain and open it as a user who has access to the dashboard and the agent.

## Notes

- Users must be signed in to AWS Quick (or authenticated through your identity provider) for registered-user embeds. Anonymous embedding needs capacity pricing.
- Keep the page behind your own access control; it shows worker-level findings.
- Embedding UI labels change between releases. If the menu names above differ in your account, search the AWS Quick user guide for "embed" and follow the current steps; the page layout does not depend on them.
