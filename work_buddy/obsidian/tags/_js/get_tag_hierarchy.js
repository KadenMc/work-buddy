// Build a tag hierarchy tree from all vault tags.
return (async () => {
    const rawTags = app.metadataCache.getTags();
    const tree = {};

    for (const [tag, count] of Object.entries(rawTags)) {
        const parts = tag.replace(/^#/, '').split('/');
        let node = tree;
        for (let i = 0; i < parts.length; i++) {
            const part = parts[i];
            if (!node[part]) {
                node[part] = {_count: 0, _tag: '#' + parts.slice(0, i + 1).join('/'), _children: {}};
            }
            if (i === parts.length - 1) {
                node[part]._count = count;
            }
            node = node[part]._children;
        }
    }

    // Flatten to a serializable structure
    function flatten(node, depth) {
        const result = [];
        for (const [name, data] of Object.entries(node)) {
            const children = flatten(data._children, depth + 1);
            result.push({
                name,
                tag: data._tag,
                count: data._count,
                depth,
                child_count: children.length,
                children
            });
        }
        return result.sort((a, b) => b.count - a.count);
    }

    return flatten(tree, 0);
})()
