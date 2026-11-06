# Admin guide

How to do common jobs in the store admin. Written from the admin's own screens and from the default
(starter) theme's section files; a custom theme may use other labels. Each entry is one task.
Format: `task`, `keywords`, `open` (a key from actions.py, never a path), `steps`, and an optional `note`.
Every label below is quoted from the admin source or a theme file; do not add one that is not.

## logo-text
task: Change the logo text in the header
keywords: logo, logo text, brand name, store name, header text, name in header, site name
open: customize
steps:
1. Open the Online Store page and click "Edit theme" on your theme.
2. In the list on the left, under "Header", click the "Header" row (Announcement, logo and navigation).
3. Find "Logo text" and type your text.
4. Wait a moment: changes in the customizer are saved automatically.
note: Leave "Logo text" empty to show your store name. If "Logo image" is set, the image is shown instead of the text. The file behind this is sections/header.liquid.

## logo-image
task: Use a logo image in the header
keywords: logo image, logo picture, upload logo, change logo, logo size, logo height, logo position, logo left, logo centre
open: customize
steps:
1. Open the Online Store page and click "Edit theme" on your theme.
2. Under "Header", click the "Header" row.
3. Set "Logo image" to your picture.
4. Use "Logo image height" to change its size, and "Logo position" to choose Left or Centred.
note: The image is shown instead of the logo text. Changes save automatically.

## announcement-bar
task: Change or hide the announcement bar
keywords: announcement, announcement bar, banner, top banner, notice, promo bar, announcement text, free delivery banner
open: customize
steps:
1. Open the Online Store page and click "Edit theme" on your theme.
2. Under "Header", click the "Header" row.
3. Type your message in "Announcement text".
4. Optional: "Announcement link" makes the whole banner a link. "Announcement colour" and "Announcement text colour" change the colours.
5. To hide the bar, clear "Announcement text".
note: The banner sits above the header. Changes save automatically. The file behind this is sections/header.liquid.

## header-menu-links
task: Add or change the links in the header menu
keywords: header menu, navigation, nav, menu link, header links, add link, top menu, main menu links
open: customize
steps:
1. Open the Online Store page and click "Edit theme" on your theme.
2. Under "Header", click the "Header" row.
3. In "Blocks", click "+ Add block" and choose "Menu link".
4. Set "Label" (the text shoppers see) and "Link" (a page of your store such as /collections/all, or a full web address).
5. Click an existing "Menu link" block to change its "Label" or "Link".
note: The header has room for up to 12 blocks. "Show the navigation menu" turns the menu on or off. With no menu links, "Collections in the automatic menu" sets how many collections are listed. Changes save automatically.

## footer
task: Edit the footer
keywords: footer, copyright, utility bar, footer links, footer text, footer logo, contact details in footer, phone in footer, bottom of page
open: customize
steps:
1. Open the Online Store page and click "Edit theme" on your theme.
2. In the list on the left, click the "Footer" row (Links, social and copyright).
3. For the line at the very bottom, change "Copyright line". Leave it empty to show the year and the store name.
4. For a thin strip with contact details, tick "Show the utility bar", then fill "Utility bar text", "Phone number" and "Email address".
5. For the footer's content, use "Blocks" and add as many as you like: "Logo and text", "Menu column", "Social links", "Text", "Newsletter" and "Payment icons". In a "Menu column", add links and pick where each goes, or choose "A menu in Admin > Menus" to follow one of your menus. In "Social links", add a network and its address for each icon.
6. For the line at the bottom, tick which policies to link. "Accordion on mobile" folds the menu columns on phones.
note: Changes save automatically. The file behind this is sections/footer.liquid.

## edit-theme-code
task: Edit theme code (the code editor)
keywords: code, theme code, edit code, code editor, liquid, css, javascript, html, template file, edit file, custom code
open: code
steps:
1. Open the Online Store page, click the "⋯" menu on your theme and choose "Edit code".
2. In the file list on the left, open a folder (layout, sections, snippets or templates) and click a file. The "Search" box finds files.
3. Edit the file in the editor.
4. Click "Save" (or press Ctrl+S, Cmd+S on a Mac).
5. To go back, click "Return to theme customizer".
note: A saved file is live on your store straight away. "History" shows the last 20 versions of the open file. Only .liquid, .css, .js and .json files are allowed. "New File" and "New Folder" are at the top of the file list.

## edit-header-footer-code
task: Edit the header or footer in code
keywords: header code, footer code, header.liquid, footer.liquid, edit header html, edit footer html, header file, footer file
open: code
steps:
1. Open the Online Store page, click the "⋯" menu on your theme and choose "Edit code".
2. Open the "sections" folder.
3. For the header (announcement bar, logo, navigation) open "header.liquid". For the footer open "footer.liquid".
4. Edit the file and click "Save".
note: These two files also define the options shown in the customizer. After you save, the customizer's Header or Footer options follow the file. The customizer's "Header" and "Footer" rows also have an "Edit code" link that opens the same file.

## add-product
task: Add a product
keywords: add product, new product, create product, product, list a bottle, add item, upload product, new item
open: add_product
steps:
1. Open "Products" and click "Add product".
2. Under "Basic info", fill in "Product name" and "About this product".
3. Add pictures under "Media".
4. Under "Pricing", set "Selling price".
5. Under "Stock", set "SKU" and, for each location, the "Quantity".
6. Under "Organize", choose "Category", "Product type" and "Vendor".
7. Under "Visibility", choose "Active" (shoppers can find and buy it), "Draft" (only you can see it) or "Archived".
8. Under "Sales channels", choose where to sell it, then click "Save product".
note: Choose "Draft" if the product is not ready to sell.

## product-price-visibility
task: Change a product's price or hide it from the store
keywords: change price, edit price, update price, price, hide product, unpublish, draft, archive product, visibility, edit product
open: products
steps:
1. Open "Products" and click the product.
2. To change the price, edit "Selling price" under "Pricing".
3. To hide it, under "Visibility" choose "Draft" (only you can see it) or "Archived" (hidden and kept for your records). Choose "Active" to show it again.
4. Click "Save changes".

## create-discount
task: Create a discount or coupon code
keywords: discount, coupon, promo, promo code, discount code, offer, sale, percentage off, amount off, free shipping, buy x get y, voucher
open: discounts
steps:
1. Open "Discounts" and click "Create discount".
2. Choose the type: "Amount off products", "Buy X get Y", "Amount off order" or "Free shipping".
3. Under "Method" choose "Discount code" (shoppers type a code; "Generate random code" makes one) or "Automatic discount" (applied by itself; give it a "Title").
4. Under "Value" choose "Percentage (%)" or "Fixed amount ($)" and enter the amount.
5. Optional: "Minimum purchase requirements", "Maximum discount uses", and "Active dates" (set a "Start date").
6. Click "Save discount".
note: "Amount off products" and "Amount off order" have "Value". "Buy X get Y" has "Customer buys" and "Customer gets". "Free shipping" has "Countries" and "Shipping rates".

## menus
task: Create or edit a menu
keywords: menu, menus, create menu, footer menu, main menu, menu item, navigation menu, url redirect
open: menus
steps:
1. Open "Content", then "Menus".
2. Click "Create menu" (or open an existing menu to change it).
3. Fill in "Title", for example Main menu or Footer menu, and the "Handle".
4. Click "+ Add menu item", then fill in "Link name" and "Link".
5. Click "Save menu".
note: "URL redirects" on the same page sends an old address to a new one. The links shown in the theme's header are edited in the customizer (see the header menu links topic).

## add-page
task: Add a page such as About or Contact
keywords: page, add page, new page, about page, contact page, policy page, create page, store page
open: pages
steps:
1. Open "Pages" and click "Add page".
2. Fill in "Title" and "Content".
3. Choose "Visible" to publish it, or "Hidden" to keep it private.
4. Click "Save".
note: A visible page appears on your store at /pages/ followed by its handle.

## publish-theme
task: Publish a different theme
keywords: publish, publish theme, make live, switch theme, change theme, go live, live theme, new theme
open: online_store
steps:
1. Open the Online Store page.
2. Find the theme under "Draft themes" (the theme in use is shown at the top of the page).
3. Click "Publish" on it.
4. Confirm the question that appears. It says which theme it replaces.
note: The theme being replaced stays in your themes as a draft.
