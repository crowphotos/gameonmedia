<?php
/**
 * Plugin Name: Expose Yoast Meta to REST
 * Description: Registers Yoast SEO focus keyphrase and meta description so they
 *              can be written via the WordPress REST API (used by the
 *              email-to-WP-drafts script). Minimal, no settings.
 * Version:     1.0
 * Author:      Johnnie
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit; // No direct access.
}

add_action( 'init', function () {

    $fields = array(
        '_yoast_wpseo_focuskw'   => 'string', // Focus keyphrase
        '_yoast_wpseo_metadesc'  => 'string', // Meta description
    );

    foreach ( $fields as $key => $type ) {
        register_post_meta( 'post', $key, array(
            'type'          => $type,
            'single'        => true,
            'show_in_rest'  => true,
            // Only allow writes from users who can edit posts.
            'auth_callback' => function () {
                return current_user_can( 'edit_posts' );
            },
        ) );
    }
} );
