<?php
// Run once after each fresh WXR import: php apply_branding.php
// (from the WordPress root, next to wp-load.php). Finishes what
// WXR itself has no mechanism for -- the site logo and real
// brand fonts are WordPress options/theme mods and a wp_head
// stylesheet link, not posts or terms.
require_once(__DIR__ . '/wp-load.php');

// Site logo -- the file itself already came in as an attachment
// via the WXR import, normally at post_id 40004 (the WXR
// wp:post_id, which the importer honours on a clean import). Fall back
// to the '_webstractor_site_logo' marker meta if that id isn't the logo.
$logo_id = 40004;
if (get_post_type($logo_id) !== 'attachment') {
    $marked = get_posts(array(
        'post_type' => 'attachment', 'post_status' => 'inherit',
        'numberposts' => 1, 'fields' => 'ids',
        'meta_key' => '_webstractor_site_logo', 'meta_value' => '1',
    ));
    $logo_id = $marked ? (int) $marked[0] : 0;
}
if ($logo_id && get_post_type($logo_id) === 'attachment') {
    update_option('site_logo', $logo_id);       // block themes' core/site-logo
    set_theme_mod('custom_logo', $logo_id);     // classic-theme fallback
    echo "Site logo set (attachment {$logo_id}).\n";
} else {
    echo "Logo attachment not found -- import the WXR file first (with 'Download and import file attachments' checked) before running this script.\n";
}

// Real brand fonts -- theme.json/wp_global_styles only *register*
// font-family names; nothing else loads the actual font files, so
// every role using one silently falls back to its generic fallback
// (e.g. Georgia/serif) without this. Written as a must-use plugin
// so it keeps loading on every future request, not just this run.
$mu_dir = WPMU_PLUGIN_DIR;
if (!file_exists($mu_dir)) {
    wp_mkdir_p($mu_dir);
}
$mu_plugin = <<<'PHP'
<?php
/* Plugin Name: Migration Brand Fonts (auto-generated) */
add_action('wp_head', function () {
    echo '<link rel="preconnect" href="https://fonts.googleapis.com">' . PHP_EOL;
    echo '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' . PHP_EOL;
    echo '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400&family=Cabin:wght@400;600&display=swap">' . PHP_EOL;
}, 1);
PHP;
file_put_contents($mu_dir . '/migration-brand-fonts.php', $mu_plugin);
echo "Brand fonts wired up via a must-use plugin (" . $mu_dir . "/migration-brand-fonts.php).\n";
