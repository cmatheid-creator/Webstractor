<?php
/**
 * Plugin Name: Stratecon Migration Repair
 * Description: One-time post-import cleanup the WXR import can't do itself -- repoints broken re-hosted image URLs, sideloads the GoDaddy stock images the importer can't take, sets the static front page, sets the site logo, and reclaims the /privacy-policy/ slug from WordPress's sample page. Runs once on activation, shows a report, then deactivates itself. Safe to activate again.
 * Version:     1.0.0
 * Author:      Webstractor migration pipeline (auto-generated)
 */

if (!defined('ABSPATH')) {
    // No WordPress around us -- this is a direct CLI run, e.g.
    //   php wp-content/plugins/repair-migration/repair-migration.php
    // (handy where a shell IS available; the plugin path is for hosts
    // where it isn't).
    if (PHP_SAPI !== 'cli') {
        exit;
    }
    $candidates = array(
        dirname(__FILE__, 4) . '/wp-load.php',  // wp-content/plugins/<dir>/<file>
        dirname(__FILE__, 3) . '/wp-load.php',
        dirname(__FILE__, 2) . '/wp-load.php',
        dirname(__FILE__) . '/wp-load.php',
    );
    $loaded = false;
    foreach ($candidates as $wp_load) {
        if (file_exists($wp_load)) {
            require_once $wp_load;
            $loaded = true;
            break;
        }
    }
    if (!$loaded) {
        fwrite(STDERR, "wp-load.php not found relative to " . __FILE__ . "\n");
        exit(1);
    }
    foreach (stratecon_migration_repair_run() as $line) {
        echo $line . "\n";
    }
    exit(0);
}

register_activation_hook(__FILE__, function () {
    // Clear any stale report first -- on hosts with a persistent object
    // cache (SiteGround's Memcached/Redis), a previous run's transient
    // can outlive the DB reset and be shown instead of this run's.
    delete_transient('stratecon_migration_repair_report');
    // Stash the report for the admin notice below. No echo here: any
    // output during activation trips WordPress's "plugin generated N
    // characters of unexpected output" warning.
    set_transient(
        'stratecon_migration_repair_report',
        stratecon_migration_repair_run(),
        10 * MINUTE_IN_SECONDS
    );
});

add_action('admin_notices', function () {
    $report = get_transient('stratecon_migration_repair_report');
    if ($report === false) {
        return;
    }
    delete_transient('stratecon_migration_repair_report');
    // Arm a one-request-later self-deactivate: the notice is shown once,
    // then the plugin bows out on its own on the next admin page load.
    // Nothing of it runs on a normal request, but no reason to leave it
    // sitting in the active list either.
    update_option('stratecon_migration_repair_cleanup', 1, false);
    echo '<div class="notice notice-success"><p><strong>' . esc_html('Stratecon Migration Repair') . ' &mdash; done.</strong></p><ul style="list-style:disc;margin-left:2em">';
    foreach ((array) $report as $line) {
        echo '<li>' . esc_html($line) . '</li>';
    }
    echo '</ul><p>This plugin has finished its one-time job and will deactivate itself. You can delete it.</p></div>';
});

add_action('admin_init', function () {
    if (!get_option('stratecon_migration_repair_cleanup')) {
        return;
    }
    delete_option('stratecon_migration_repair_cleanup');
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
    deactivate_plugins(plugin_basename(__FILE__));
});

// Sideload one image whose URL has no usable extension (GoDaddy's
// isteam/stock/<id> URLs): download, sniff the real type, name the temp
// file ourselves, then hand it to media_handle_sideload(). Declared
// unconditionally at file scope so PHP hoists it -- the CLI branch above
// calls run() before this point in the file is ever reached.
function stratecon_migration_repair_sideload($src, $alt) {
    $tmp = download_url($src, 30);
    if (is_wp_error($tmp)) {
        return $tmp;
    }
    $info = @getimagesize($tmp);
    $ext  = $info ? ltrim(image_type_to_extension($info[2]), '.') : 'jpg';
    $file_array = array(
        'name'     => 'stock-' . substr(md5($src), 0, 12) . '.' . $ext,
        'tmp_name' => $tmp,
    );
    $id = media_handle_sideload($file_array, 0, $alt !== '' ? $alt : null);
    if (is_wp_error($id)) {
        @unlink($tmp);
        return $id;
    }
    return wp_get_attachment_url($id);
}

function stratecon_migration_repair_run() {
    @set_time_limit(300);
    @ignore_user_abort(true);
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $report = array();
    $uploads = wp_get_upload_dir();
    $all_posts = get_posts(array(
        'post_type'   => array('page', 'post'),
        'post_status' => 'any',
        'numberposts' => -1,
    ));

    // -----------------------------------------------------------------
    // 1. Repoint broken /wp-content/uploads/ image URLs at the real file.
    // -----------------------------------------------------------------
    $img_url_re = '~https?://[^\s\x22\x27<>()]+?/wp-content/uploads/[^\s\x22\x27<>()]+?\.(?:jpe?g|png|gif|webp|avif)(?=[\s\x22\x27>)]|$)~i';
    $fixed_refs = 0;
    foreach ($all_posts as $post) {
        $content = $post->post_content;
        if (strpos($content, '/wp-content/uploads/') === false) {
            continue;
        }
        $updated = $content;
        if (preg_match_all($img_url_re, $content, $m)) {
            foreach (array_unique($m[0]) as $url) {
                $rel  = ltrim(str_replace($uploads['baseurl'], '', $url), '/');
                $path = $uploads['basedir'] . '/' . $rel;
                if (file_exists($path)) {
                    continue;  // URL already resolves -- nothing to do
                }
                $dir  = dirname($path);
                $stem = preg_replace('/\.[a-z0-9]+$/i', '', basename($path));
                // Same stem, any real image extension; also tolerate
                // WordPress's -1/-2 filename-collision suffix on the real
                // file. Prefer an exact-stem match; fall back to a
                // suffixed one. Skip WordPress's own -WxH sub-sizes.
                $candidates = array_merge(
                    (array) glob($dir . '/' . $stem . '.*'),
                    (array) glob($dir . '/' . $stem . '-*.*')
                );
                $replacement = null;
                foreach ($candidates as $cand) {
                    if (preg_match('/-\d+x\d+\.[a-z0-9]+$/i', $cand)) {
                        continue;  // WordPress sub-size, not the original
                    }
                    if (preg_match('/\.(jpe?g|png|gif|webp|avif)$/i', $cand) && is_file($cand)) {
                        $replacement = $uploads['baseurl'] . '/' . ltrim(str_replace($uploads['basedir'], '', $cand), '/');
                        break;
                    }
                }
                if ($replacement && $replacement !== $url) {
                    $updated = str_replace($url, $replacement, $updated);
                    $fixed_refs++;
                }
            }
        }
        if ($updated !== $content) {
            wp_update_post(array('ID' => $post->ID, 'post_content' => $updated));
        }
    }
    $report[] = "Broken image URLs repointed: {$fixed_refs}";

    // -----------------------------------------------------------------
    // 2. Sideload stock images the importer couldn't, then repoint refs.
    // -----------------------------------------------------------------
    $stock = array(
        'https://img1.wsimg.com/isteam/stock/2646/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:1535,m' => 'An open hand cupping a floating globe made of digital elements, representing technology',
        'https://img1.wsimg.com/isteam/stock/10130/:/cr=t:0%25,l:22.46%25,w:55.08%25,h:100%25/rs=w:365,h:365,cg:true' => 'Picture of a man in a business suit with his finger pointing at an icon of a person wearing a tie',
        'https://img1.wsimg.com/isteam/stock/Q3VZ7AA/:/cr=t:0%25,l:17.57%25,w:64.86%25,h:100%25/rs=w:365,h:365,cg:true' => 'Picture of a black female pointing at a glass whiteboard covered in sticky notes',
        'https://img1.wsimg.com/isteam/stock/3250/:/cr=t:0%25,l:15%25,w:70%25,h:99.99%25/rs=w:365,h:365,cg:true,m' => 'Man standing in front of a large chalkboard that is filled with pictures of charts,  workflows',
        'https://img1.wsimg.com/isteam/stock/BxpR1ED/:/cr=t:0%25,l:16.67%25,w:66.67%25,h:100%25/rs=w:365,h:365,cg:true' => 'Hand touching a screen with outlines of two brains',
        'https://img1.wsimg.com/isteam/stock/yrdogW5/:/cr=t:0%25,l:16.67%25,w:66.67%25,h:100%25/rs=w:365,h:365,cg:true' => 'Smiling man with a headset on sitting in front of a computer ',
        'https://img1.wsimg.com/isteam/stock/359/:/cr=t:0%25,l:19.81%25,w:60.38%25,h:100%25/rs=w:365,h:365,cg:true' => 'Cybersecurity image with a lock, risk alert, and hacking detected text',
        'https://img1.wsimg.com/isteam/stock/25097/:/cr=t:0%25,l:0%25,w:100%25,h:100%25' => 'Man in business attire pointing at interconnected circles representing different cities',
        'https://img1.wsimg.com/isteam/stock/4684/:/cr=t:0%25,l:10.1%25,w:79.8%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Diagram of countries across the world with people icons interconnected between them',
        'https://img1.wsimg.com/isteam/stock/Qp0ewAK/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Picture of a digital globe resting on a laptop keyboard',
        'https://img1.wsimg.com/isteam/stock/101189/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Virtual diagram of people connected in a tree pattern. A business person is pointing at one person ',
        'https://img1.wsimg.com/isteam/stock/dY9drgY/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Virtual diagram of computer icons connected in a network with a padlock in the cloud',
        'https://img1.wsimg.com/isteam/stock/xrjYk18/:/cr=t:0%25,l:5.7%25,w:88.6%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Woman\'s hands on a laptop with mouse in foreground and coffee cup in the background',
        'https://img1.wsimg.com/isteam/stock/wVOw01Y/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25' => 'Asian business woman speaking into the mobile phone she is holding in her hand',
        'https://img1.wsimg.com/isteam/stock/12136/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Group of people gathered around a wooden table with laptops, notepads, and pens',
        'https://img1.wsimg.com/isteam/stock/pY8WwV7/:/cr=t:0%25,l:5.61%25,w:88.77%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Business man wearing a tie leaning over the shoulder of the other business man that he is coaching',
        'https://img1.wsimg.com/isteam/stock/7373P7n/:/cr=t:12.37%25,l:0%25,w:100%25,h:75.27%25/rs=w:600,h:300,cg:true' => 'Hands of a man in business attire on a computer keyboard',
        'https://img1.wsimg.com/isteam/stock/817/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Image of computer screen with lettes and numbers in neon type and \'system protected\' message',
        'https://img1.wsimg.com/isteam/stock/2957/:/cr=t:10.69%25,l:0%25,w:100%25,h:78.61%25/rs=w:388,h:194,cg:true' => 'Business person pointing at Compliance in a list of words floating in front of them',
        'https://img1.wsimg.com/isteam/stock/BxO78pD/:/cr=t:7.45%25,l:0%25,w:100%25,h:85.1%25/rs=w:388,h:194,cg:true' => 'Business person\'s hand on a glass keyboard with pictures of technology floating around a lock',
        'https://img1.wsimg.com/isteam/stock/366/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Virtual image of a padlock with icons around it representing data and devices',
        'https://img1.wsimg.com/isteam/stock/2456/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Red emergency key on computer keyboard',
        'https://img1.wsimg.com/isteam/stock/DxdyDbB/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Stethoscope held up to a computer hard drive',
        'https://img1.wsimg.com/isteam/stock/u4m1pJgJgOSrPzge3/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Female instructor looking over the shoulder of her student',
        'https://img1.wsimg.com/isteam/stock/oANb51Q/:/cr=t:0%25,l:0%25,w:100%25,h:100%25' => 'Pair of hands holding a glowing globe',
        'https://img1.wsimg.com/isteam/stock/6681/:/cr=t:0%25,l:0.66%25,w:98.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Woman smiling and working with coworkers at a table',
        'https://img1.wsimg.com/isteam/stock/xr81V4d/:/cr=t:0%25,l:5.61%25,w:88.77%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Many in business suit pointing at interconnected diagram of people',
        'https://img1.wsimg.com/isteam/stock/1749/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Pair of reading glasses being held in front of an eye chart',
        'https://img1.wsimg.com/isteam/stock/uENxz754EaI35BQJA/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Black man with glasses at a coffee shop looking at his laptop',
        'https://img1.wsimg.com/isteam/stock/4701/:/cr=t:9.69%25,l:0%25,w:100%25,h:80.62%25' => 'Man in business shirt pointing to a floating list with Excellent checked',
        'https://img1.wsimg.com/isteam/stock/2957/:/cr=t:0%25,l:7.7%25,w:84.59%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Man in business suit pointing to floating words with Compliance highlighted',
        'https://img1.wsimg.com/isteam/stock/363/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Computer screen image with the phrases "Data Breach" and "Cyber Attack" highlighted',
        'https://img1.wsimg.com/isteam/stock/817/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Computer screen with the phrase "System Protected" highlighted',
        'https://img1.wsimg.com/isteam/stock/7wP9GK4/:/cr=t:0%25,l:12.61%25,w:74.78%25,h:100%25' => 'Business woman with glasses holding a digital tablet with computer screens shown in the background',
        'https://img1.wsimg.com/isteam/stock/xxk54z8/:/cr=t:0%25,l:12.61%25,w:74.78%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Business man seated in front of five  computer screens full of connectivity maps and data',
        'https://img1.wsimg.com/isteam/stock/NrgJkqm/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25' => 'Business person holding a magnifying glass over a laptop',
        'https://img1.wsimg.com/isteam/stock/7wP9GK4/:/cr=t:0%25,l:12.61%25,w:74.78%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Business woman with glasses holding a digital tablet with computer screens shown in the background',
        'https://img1.wsimg.com/isteam/stock/861/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Woman holding a magnifying glass up to a virtual picture of people icons',
        'https://img1.wsimg.com/isteam/stock/dY9drgY/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Digital picture of computer devices networked together, all connecting to cloud w/a padlock in it',
        'https://img1.wsimg.com/isteam/stock/Dx4PWZa/:/cr=t:13.6%25,l:0%25,w:100%25,h:72.79%25' => 'Light bulb held in a person\'s hand with icons representing power sources',
        'https://img1.wsimg.com/isteam/stock/281/:/cr=t:0%25,l:6.47%25,w:87.05%25,h:99.99%25/rs=w:600,h:451.12781954887214,cg:true,m' => 'Business person holding a digital tablet with images of digital devices and globe  hovering above',
        'https://img1.wsimg.com/isteam/stock/DxRYKJo/:/cr=t:0%25,l:12.61%25,w:74.78%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Man and woman in business attire walking down a row of equipment in a data center',
        'https://img1.wsimg.com/isteam/stock/1157/:/cr=t:0%25,l:15.52%25,w:68.97%25,h:100%25/rs=w:600,h:600,cg:true' => 'Business man in a suite cupping his hand under floating images of people and bar charts',
        'https://img1.wsimg.com/isteam/stock/WVDERZr/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Man standing in front of virtual digital screens with an evening city skyscape in the background',
        'https://img1.wsimg.com/isteam/stock/kZYay22/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Business person\'s hand with a marker pointing at a dot plot graph',
        'https://img1.wsimg.com/isteam/stock/WbzGo2j/:/cr=t:16.66%25,l:0%25,w:100%25,h:66.69%25/rs=w:388,h:194,cg:true' => '3D bar and pie charts hovering above a digital tablet',
        'https://img1.wsimg.com/isteam/stock/548/:/cr=t:0%25,l:6.77%25,w:86.47%25,h:100%25' => 'Picture of the hood of a car and blurred lights streaming by, indicating speed',
        'https://img1.wsimg.com/isteam/stock/xqPA0zB/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Female and male customer service agents with headsets on, working in front of their computers',
        'https://img1.wsimg.com/isteam/stock/uV8DKZOAJjIJJEGz7/:/cr=t:25%25,l:0%25,w:100%25,h:50%25/rs=w:388,h:194,cg:true' => 'Hand holding a mobile phone with a web page  about "most inspiring places" on display',
        'https://img1.wsimg.com/isteam/stock/101189/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Virtual diagram of people connected in a tree pattern. A business person is pointing at one person ',
        'https://img1.wsimg.com/isteam/stock/4700/:/cr=t:18.75%25,l:0%25,w:100%25,h:62.5%25/rs=w:388,h:194,cg:true' => 'Compass with the north point listed as Quality',
        'https://img1.wsimg.com/isteam/stock/npb1rN2/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:388,h:194,cg:true' => 'Person\'s hand on a laptop keyboard',
        'https://img1.wsimg.com/isteam/stock/285/:/cr=t:16.7%25,l:0%25,w:100%25,h:66.6%25/rs=w:388,h:194,cg:true' => 'Business person holding a tablet with virtual digital images hovering above it',
        'https://img1.wsimg.com/isteam/stock/358/:/cr=t:0%25,l:0%25,w:100%25,h:100%25' => 'Business person holding their mobile phone while resting their hands on their laptop',
        'https://img1.wsimg.com/isteam/stock/YkK0pVJ/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Digital tablet and mobile phone resting on top of a computer laptop',
        'https://img1.wsimg.com/isteam/stock/855/:/cr=t:0%25,l:18.49%25,w:63.02%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Business man in a suit pointing at a digital image with the faces of many people',
        'https://img1.wsimg.com/isteam/stock/wNNnjbP/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Hand putting a coin in piggy bank with chalkboard in the background showing bar chart ',
        'https://img1.wsimg.com/isteam/stock/xVyraOq/:/cr=t:0%25,l:0%25,w:100%25,h:100%25' => 'Woman and man wearing headsets and sitting in front of computers',
        'https://img1.wsimg.com/isteam/stock/242/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Many holding a table that has a graph showing up and to the right growth over time',
        'https://img1.wsimg.com/isteam/stock/425/:/cr=t:0%25,l:1.42%25,w:97.17%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'You woman with a headset in front of computer, smiling at camera',
        'https://img1.wsimg.com/isteam/stock/10130/:/cr=t:0%25,l:13.37%25,w:73.25%25,h:99.99%25/rs=w:600,h:451.12781954887214,cg:true,m' => 'Man in business suit pointing at diagram of people and technology connected together',
        'https://img1.wsimg.com/isteam/stock/ka6pG8G/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Man in business suit with hands on tablet and image of digital globe floating in front',
        'https://img1.wsimg.com/isteam/stock/yrdogW5/:/cr=t:0%25,l:5.67%25,w:88.67%25,h:100%25/rs=w:600,h:451.12781954887214,cg:true' => 'Man with headset dressed in business attire sitting in front of computer',
        'https://img1.wsimg.com/isteam/stock/4700/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Compass with needle pointing to Quality on the dial',
        'https://img1.wsimg.com/isteam/stock/378/:/cr=t:13.96%25,l:0%25,w:100%25,h:72.08%25/rs=w:600,h:300,cg:true' => 'Man in business suit holding digital images of technology',
        'https://img1.wsimg.com/isteam/stock/7zn5E1m/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25/rs=w:600,h:300,cg:true' => 'Man and woman in business attire looking at a computer screen together',
        'https://img1.wsimg.com/isteam/stock/3258/:/cr=t:0%25,l:1.48%25,w:97.03%25,h:100%25/rs=w:600,h:300,cg:true' => 'Man in business suit pointing at floating image of interconnected circles',
        'https://img1.wsimg.com/isteam/stock/861/:/cr=t:12.5%25,l:0%25,w:100%25,h:75%25' => 'Magnifying glass with people icons inside and all around signifying customer experience',
        'https://img1.wsimg.com/isteam/stock/7373P7n/:/cr=t:0%25,l:0%25,w:100%25,h:100%25/rs=w:600,cg:true' => 'Customer service agent typing on a keyboard',
        'https://img1.wsimg.com/isteam/stock/4700/:/cr=t:3.01%25,l:0%25,w:100%25,h:93.98%25/rs=w:600,h:451.12781954887214,cg:true' => 'Compass with the arrow pointing to Quality, referring to Quality Management',
        'https://img1.wsimg.com/isteam/stock/366/:/cr=t:0%25,l:0%25,w:100%25,h:100%25' => 'Digital image with a padlock in the middle and various icons around it representing documents, devic'
    );
    // Longest URL first. Some of these keys are a strict prefix of
    // another (the same GoDaddy image referenced once with a
    // ".../rs=w:600,..." resize suffix and once without): matching the
    // shorter one first replaces only part of the longer one's <img
    // src>, leaving a dangling ".../rs=w:600,..." that 404s. Handling
    // the more specific URL first, then re-checking, avoids that.
    uksort($stock, function ($a, $b) { return strlen($b) - strlen($a); });
    $sideloaded = 0;
    $stock_skipped = 0;
    foreach ($stock as $src => $alt) {
        $still_used = false;
        foreach ($all_posts as $post) {
            if (strpos(get_post_field('post_content', $post->ID), $src) !== false) {
                $still_used = true;
                break;
            }
        }
        if (!$still_used) {
            $stock_skipped++;
            continue;  // already handled on a previous run, or never referenced
        }
        $new_url = stratecon_migration_repair_sideload($src, $alt);
        if (is_wp_error($new_url)) {
            $report[] = "  stock sideload failed ({$src}): " . $new_url->get_error_message();
            continue;
        }
        foreach ($all_posts as $post) {
            $content = get_post_field('post_content', $post->ID);
            if (strpos($content, $src) !== false) {
                wp_update_post(array('ID' => $post->ID, 'post_content' => str_replace($src, $new_url, $content)));
            }
        }
        $sideloaded++;
    }

    // A local uploads image URL is sometimes left with a trailing GoDaddy
    // transform suffix ("stock-x.jpeg/rs=w:600,..." / ".../:/cr=...") --
    // e.g. when a shorter stock URL matched as a prefix of a longer one,
    // or the importer sanitised the URL so the full stock key no longer
    // matched. Nothing legitimate follows an image extension with "/rs="
    // or "/cr=" or "/:", so chop any of that off.
    $trail_re = '~(/wp-content/uploads/[^\s\x22\x27<>()]+?\.(?:jpe?g|png|gif|webp|avif))/(?:rs=|cr=|:)[^\s\x22\x27<>()]*~i';
    $trimmed = 0;
    foreach ($all_posts as $post) {
        $content = get_post_field('post_content', $post->ID);
        $nc = preg_replace($trail_re, '$1', $content);
        if ($nc !== null && $nc !== $content) {
            wp_update_post(array('ID' => $post->ID, 'post_content' => $nc));
            $trimmed++;
        }
    }
    $report[] = "Stock images sideloaded: {$sideloaded} (skipped {$stock_skipped} already done/unused)"
        . ($trimmed ? "; trimmed a stray transform suffix on {$trimmed} page(s)" : "");

    // -----------------------------------------------------------------
    // 3. Static front page.
    // -----------------------------------------------------------------
    $front_slug = 'home';
    if ($front_slug) {
        $front = get_page_by_path($front_slug);
        if ($front) {
            update_option('show_on_front', 'page');
            update_option('page_on_front', $front->ID);
            $note = ($front->post_status === 'publish') ? '' : " (still a {$front->post_status} -- publish it so / resolves)";
            $report[] = "Front page set to \"{$front->post_title}\" (slug {$front_slug}, id {$front->ID}){$note}.";
        } else {
            $report[] = "Front-page slug \"{$front_slug}\" not found -- publish the imported pages first, then activate this plugin again.";
        }
    }

    // -----------------------------------------------------------------
    // 4. Site logo. The file came in as a WXR attachment; "which
    //    attachment is the logo" is the site_logo option + custom_logo
    //    theme mod, which no WXR item can carry and a full reset wipes.
    // -----------------------------------------------------------------
    $logo_id = 40004;  // the WXR wp:post_id; 0 if the site has no logo
    if ($logo_id) {
        if (get_post_type($logo_id) !== 'attachment') {
            // The fixed WXR post id didn't survive import (id already
            // taken, or this wasn't a clean import over an empty DB).
            // Fall back to the marker the generator stamps on the logo.
            $marked = get_posts(array(
                'post_type'   => 'attachment',
                'post_status' => 'inherit',
                'numberposts' => 1,
                'fields'      => 'ids',
                'meta_key'    => '_webstractor_site_logo',
                'meta_value'  => '1',
            ));
            $logo_id = $marked ? (int) $marked[0] : 0;
        }
        if ($logo_id && get_post_type($logo_id) === 'attachment') {
            update_option('site_logo', $logo_id);    // block themes (core/site-logo)
            set_theme_mod('custom_logo', $logo_id);  // classic-theme fallback
            $report[] = "Site logo set (attachment {$logo_id}).";
        } else {
            $report[] = "Site logo NOT set -- logo attachment not found. Import the WXR with \"Download and import file attachments\" checked, then activate this plugin again, or set it by hand in Appearance -> Editor -> Styles.";
        }
    }

    // -----------------------------------------------------------------
    // 5. Reclaim the /privacy-policy/ slug. WordPress (and a WP Reset)
    //    seed a sample "Privacy Policy" page, so the WXR import can't
    //    claim that slug and the migrated page lands at
    //    /privacy-policy-2/. Detect that -- the sample page always carries
    //    the "Suggested text:" boilerplate -- and clean it up: trash the
    //    sample and put the migrated page on /privacy-policy/ if it isn't
    //    already there, then repoint the privacy-policy option.
    //    Which of the two ends up with the "-2" slug depends on the order
    //    they were published (if the operator publishes WordPress's own
    //    sample draft alongside the imported pages, the migrated one can
    //    grab /privacy-policy/ directly), so trashing the sample must NOT
    //    be gated on the migrated page's current slug -- only the re-slug
    //    step is. The leftover /privacy-policy-2/ (WordPress's, never a
    //    real crawled URL) simply 404s afterwards; core's _wp_old_slug
    //    redirect doesn't apply across two different posts for a bare
    //    page URL. Idempotent: get_posts(post_status=any) doesn't return
    //    trashed pages, so a second run finds no sample and does nothing.
    // -----------------------------------------------------------------
    $pp_pages = get_posts(array(
        'post_type'   => 'page',
        'post_status' => 'any',
        'title'       => 'Privacy Policy',
        'numberposts' => 10,
    ));
    $pp_sample = null;
    $pp_migrated = null;
    foreach ($pp_pages as $pp) {
        if (strpos((string) $pp->post_content, 'Suggested text:') !== false) {
            $pp_sample = $pp;
        } elseif (!$pp_migrated) {
            $pp_migrated = $pp;
        }
    }
    if ($pp_migrated && $pp_sample && (int) $pp_migrated->ID !== (int) $pp_sample->ID) {
        wp_trash_post($pp_sample->ID);                 // appends __trashed, frees the slug
        $moved = false;
        if ($pp_migrated->post_name !== 'privacy-policy') {
            wp_update_post(array('ID' => $pp_migrated->ID, 'post_name' => 'privacy-policy'));
            $moved = true;
        }
        if ((int) get_option('wp_page_for_privacy_policy') === (int) $pp_sample->ID) {
            update_option('wp_page_for_privacy_policy', $pp_migrated->ID);
        }
        $report[] = "Privacy Policy: trashed the WordPress sample page (id {$pp_sample->ID}); "
                  . ($moved ? "moved the migrated page to /privacy-policy/." : "migrated page already at /privacy-policy/.");
    }

    return $report;
}
